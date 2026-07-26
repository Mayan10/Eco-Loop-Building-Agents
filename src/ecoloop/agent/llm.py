"""The Ollama chat client: retry, circuit breaker, and a prompt-hash cache.

Provider-agnostic by construction (AGENTS.md: "any OpenAI-compatible server
works by changing only" ``llm.base_url``/``llm.model``) — this module speaks
Ollama's ``/api/chat`` shape, which is close enough to the OpenAI chat
completion shape that switching providers is a base-URL and payload-shape
change here, not a rewrite of anything that calls it.

Three layers of defence around a single HTTP call, all real EnergyPlus-grade
concerns transplanted to an LLM endpoint: a **circuit breaker** so a
persistently failing endpoint costs one fast rejection instead of a timeout
on every subsequent call; **retry** with jittered exponential backoff for
transient failures; and a **prompt-hash cache** so a repeated request
(the same model, messages, tools, temperature and seed) never needs the
network at all — deterministic decoding (``temperature: 0``, a fixed
``seed``) is what makes that cache key meaningful rather than misleading.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ecoloop.config import CircuitBreakerSettings, LLMSettings
from ecoloop.errors import CircuitOpenError, LLMUnavailableError
from ecoloop.logging import get_logger

__all__ = ["ChatResponse", "LLMClient", "OllamaClient", "ToolCall"]

_logger = get_logger(__name__, component="agent")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """One chat completion: either a final answer, tool calls, or both."""

    content: str
    tool_calls: tuple[ToolCall, ...]
    cached: bool = False


@runtime_checkable
class LLMClient(Protocol):
    """What ``CognitiveOrchestrator`` needs from a chat client.

    ``OllamaClient`` implements this; so do the scripted doubles in
    ``tests/fakes/fake_llm.py``. Depending on the protocol rather than the
    concrete class is what lets those doubles stand in for a real endpoint
    without inheriting from it — the same seam ``SimulationBackend`` provides
    for the fake EnergyPlus double.
    """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
    ) -> ChatResponse:
        """Send one chat completion request and return the model's response."""
        ...


class _CircuitBreaker:
    """A closed/open/half-open breaker guarding the LLM endpoint.

    Args:
        settings: Failure threshold, cooldown, and half-open probe budget.
    """

    def __init__(self, settings: CircuitBreakerSettings) -> None:
        self._settings = settings
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_probes_used = 0

    def before_call(self) -> None:
        """Raise if a call should be rejected without touching the network.

        Raises:
            CircuitOpenError: If the circuit is open and its cooldown has not
                elapsed, or if it is half-open and already spent its probe
                budget for this cooldown cycle.
        """
        if self._opened_at is None:
            return

        elapsed = time.monotonic() - self._opened_at
        if elapsed < self._settings.cooldown_seconds:
            raise CircuitOpenError(
                "circuit breaker open",
                cooldown_remaining_s=self._settings.cooldown_seconds - elapsed,
            )

        # Cooldown has elapsed: half-open, allowing a bounded number of probes.
        if self._half_open_probes_used >= self._settings.half_open_max_calls:
            raise CircuitOpenError("circuit breaker half-open; probe budget spent for this cycle")
        self._half_open_probes_used += 1

    def record_success(self) -> None:
        """Reset to fully closed after a successful call."""
        self._failure_count = 0
        self._opened_at = None
        self._half_open_probes_used = 0

    def record_failure(self) -> None:
        """Count a failure, opening the circuit once the threshold is hit."""
        self._failure_count += 1
        if self._failure_count >= self._settings.failure_threshold:
            self._opened_at = time.monotonic()
            self._half_open_probes_used = 0


class OllamaClient:
    """Chat client for Ollama's ``/api/chat`` endpoint.

    Args:
        settings: LLM endpoint, retry, circuit breaker and cache settings.
        http_client: Injected transport, for tests. A real
            ``httpx.AsyncClient`` against ``settings.base_url`` if omitted.
        cache_dir: Resolved cache directory. Callers pass the already-resolved
            path (via ``settings.resolve()``) rather than this module
            resolving it itself, keeping path resolution in one place.
    """

    def __init__(
        self,
        settings: LLMSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        """Bind this client to its settings, transport, and cache directory."""
        self._settings = settings
        self._http = http_client or httpx.AsyncClient(base_url=settings.base_url, timeout=120.0)
        self._circuit = _CircuitBreaker(settings.circuit_breaker)
        self._cache_dir = cache_dir
        if self._settings.cache.enabled and self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def model(self) -> str:
        """The default model this client sends requests to, for trace entries."""
        return self._settings.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
    ) -> ChatResponse:
        """Send a chat completion request, through the cache and circuit breaker.

        Args:
            messages: Chat messages in Ollama/OpenAI role-content shape.
            tools: Tool schemas the model may call, in OpenAI function-calling
                shape.
            model: Override for ``llm.model``.

        Returns:
            The model's response.

        Raises:
            CircuitOpenError: If the breaker is currently rejecting calls.
            LLMUnavailableError: If the call fails after retrying.
        """
        chosen_model = model or self._settings.model
        cache_key = _hash_request(
            chosen_model, messages, tools, self._settings.temperature, self._settings.seed
        )

        if self._settings.cache.enabled:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return replace(cached, cached=True)

        self._circuit.before_call()
        try:
            response = await self._call_with_retry(chosen_model, messages, tools)
        except Exception as exc:
            self._circuit.record_failure()
            _logger.warning("LLM call failed", model=chosen_model, cause=str(exc))
            raise LLMUnavailableError(
                "LLM call failed", model=chosen_model, cause=str(exc)
            ) from exc

        self._circuit.record_success()
        if self._settings.cache.enabled:
            self._write_cache(cache_key, response)
        return response

    async def _call_with_retry(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> ChatResponse:
        """Call the endpoint once, retrying transient failures with backoff.

        Args:
            model: Model name to request.
            messages: Chat messages.
            tools: Tool schemas, if any.

        Returns:
            The parsed chat response.
        """

        @retry(
            stop=stop_after_attempt(self._settings.retry.max_attempts),
            wait=wait_exponential_jitter(
                initial=self._settings.retry.backoff_base_seconds,
                max=self._settings.retry.backoff_max_seconds,
                jitter=self._settings.retry.jitter_seconds,
            ),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        async def _attempt() -> ChatResponse:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": self._settings.temperature,
                    "seed": self._settings.seed,
                    "top_p": self._settings.top_p,
                },
            }
            if tools:
                payload["tools"] = tools
            response = await self._http.post("/api/chat", json=payload)
            response.raise_for_status()
            return _parse_response(response.json())

        return await _attempt()

    def _read_cache(self, cache_key: str) -> ChatResponse | None:
        """Read a cached response, if the cache is usable and has one.

        Args:
            cache_key: The prompt-hash cache key.

        Returns:
            The cached response, or ``None`` on a cache miss or unusable cache.
        """
        if self._cache_dir is None:
            return None
        path = self._cache_dir / f"{cache_key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _logger.warning("cache entry unreadable, ignoring", cache_key=cache_key)
            return None
        return ChatResponse(
            content=data["content"],
            tool_calls=tuple(ToolCall(**tc) for tc in data["tool_calls"]),
        )

    def _write_cache(self, cache_key: str, response: ChatResponse) -> None:
        """Persist a response to the cache, evicting the oldest entry if full.

        Args:
            cache_key: The prompt-hash cache key.
            response: The response to persist.
        """
        if self._cache_dir is None:
            return
        entries = sorted(self._cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if len(entries) >= self._settings.cache.max_entries:
            entries[0].unlink(missing_ok=True)

        path = self._cache_dir / f"{cache_key}.json"
        payload = {
            "content": response.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in response.tool_calls
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")


def _hash_request(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    seed: int,
) -> str:
    """Hash everything that determines a deterministic response.

    Args:
        model: Model name.
        messages: Chat messages.
        tools: Tool schemas, if any.
        temperature: Sampling temperature.
        seed: Sampling seed.

    Returns:
        A stable hex digest, used as the cache key.
    """
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "seed": seed,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_response(data: dict[str, Any]) -> ChatResponse:
    """Parse Ollama's ``/api/chat`` response body into a ChatResponse.

    Args:
        data: The decoded JSON response body.

    Returns:
        The parsed response.
    """
    message = data.get("message", {})
    tool_calls = tuple(
        ToolCall(
            id=str(tc.get("id", "")),
            name=tc["function"]["name"],
            arguments=tc["function"].get("arguments", {}),
        )
        for tc in message.get("tool_calls", []) or []
    )
    return ChatResponse(content=message.get("content", ""), tool_calls=tool_calls)
