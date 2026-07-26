"""Tests for OllamaClient: retry, circuit breaker, and the prompt-hash cache.

All against httpx.MockTransport - no real network calls, so this runs in CI
without Ollama installed, matching AGENTS.md's "the suite runs with EnergyPlus
and Ollama absent."
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from ecoloop.agent.llm import OllamaClient
from ecoloop.config import CircuitBreakerSettings, LLMSettings
from ecoloop.errors import CircuitOpenError, LLMUnavailableError


def llm_settings(**overrides: object) -> LLMSettings:
    defaults: dict[str, object] = {
        "base_url": "http://fake",
        "model": "test-model",
        "request_timeout_seconds": 5.0,
        "connect_timeout_seconds": 2.0,
        "circuit_breaker": CircuitBreakerSettings(
            failure_threshold=2, cooldown_seconds=0.05, half_open_max_calls=1
        ),
    }
    defaults.update(overrides)
    return LLMSettings(**defaults)  # type: ignore[arg-type]


def ok_response(content: str = "hello", tool_calls: list[dict[str, object]] | None = None) -> dict:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"model": "test-model", "message": message, "done": True}


def client_with_handler(handler, **settings_overrides: object) -> OllamaClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(base_url="http://fake", transport=transport)
    return OllamaClient(llm_settings(**settings_overrides), http_client=http_client)


class TestBasicChat:
    async def test_parses_a_plain_text_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ok_response("plain answer"))

        client = client_with_handler(handler)
        response = await client.chat([{"role": "user", "content": "hi"}])
        assert response.content == "plain answer"
        assert response.tool_calls == ()
        assert response.cached is False

    async def test_parses_tool_calls(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=ok_response(
                    "",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "function": {
                                "name": "get_zone_telemetry",
                                "arguments": {"zone": "Core_ZN"},
                            },
                        }
                    ],
                ),
            )

        client = client_with_handler(handler)
        response = await client.chat([{"role": "user", "content": "hi"}])
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "get_zone_telemetry"
        assert response.tool_calls[0].arguments == {"zone": "Core_ZN"}


class TestRetry:
    async def test_transient_failure_is_retried_and_succeeds(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 2:
                return httpx.Response(503)
            return httpx.Response(200, json=ok_response("recovered"))

        client = client_with_handler(handler)
        response = await client.chat([{"role": "user", "content": "hi"}])
        assert response.content == "recovered"
        assert attempts["n"] == 2

    async def test_exhausting_retries_raises_llm_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = client_with_handler(
            handler, retry={"max_attempts": 2, "backoff_base_seconds": 0.01}
        )
        with pytest.raises(LLMUnavailableError):
            await client.chat([{"role": "user", "content": "hi"}])


class TestCircuitBreaker:
    async def test_opens_after_the_failure_threshold(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = client_with_handler(
            handler, retry={"max_attempts": 1, "backoff_base_seconds": 0.01}
        )
        for _ in range(2):  # failure_threshold=2
            with pytest.raises(LLMUnavailableError):
                await client.chat([{"role": "user", "content": "hi"}])

        with pytest.raises(CircuitOpenError):
            await client.chat([{"role": "user", "content": "hi"}])

    async def test_closes_again_after_a_success(self) -> None:
        state = {"fail": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if state["fail"]:
                return httpx.Response(503)
            return httpx.Response(200, json=ok_response("ok"))

        client = client_with_handler(
            handler, retry={"max_attempts": 1, "backoff_base_seconds": 0.01}
        )
        with pytest.raises(LLMUnavailableError):
            await client.chat([{"role": "user", "content": "hi"}])

        state["fail"] = False
        response = await client.chat([{"role": "user", "content": "hi"}])
        assert response.content == "ok"

        # A single success should not have opened the breaker - confirm a
        # second call still goes through rather than raising CircuitOpenError.
        response2 = await client.chat([{"role": "user", "content": "hi"}])
        assert response2.content == "ok"

    async def test_half_open_allows_a_bounded_probe_after_cooldown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = client_with_handler(
            handler, retry={"max_attempts": 1, "backoff_base_seconds": 0.01}
        )
        for _ in range(2):
            with pytest.raises(LLMUnavailableError):
                await client.chat([{"role": "user", "content": "hi"}])
        with pytest.raises(CircuitOpenError):
            await client.chat([{"role": "user", "content": "hi"}])

        await asyncio.sleep(0.06)  # cooldown_seconds=0.05
        # Half-open: one probe is allowed through (and fails again here).
        with pytest.raises(LLMUnavailableError):
            await client.chat([{"role": "user", "content": "hi"}])


class TestCache:
    async def test_identical_request_is_served_from_cache(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=ok_response("cached answer"))

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(base_url="http://fake", transport=transport)
        client = OllamaClient(llm_settings(), http_client=http_client, cache_dir=tmp_path)

        r1 = await client.chat([{"role": "user", "content": "hi"}])
        r2 = await client.chat([{"role": "user", "content": "hi"}])
        assert r1.cached is False
        assert r2.cached is True
        assert r2.content == "cached answer"
        assert calls["n"] == 1

    async def test_different_messages_are_not_conflated(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ok_response("answer"))

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(base_url="http://fake", transport=transport)
        client = OllamaClient(llm_settings(), http_client=http_client, cache_dir=tmp_path)

        await client.chat([{"role": "user", "content": "question A"}])
        r2 = await client.chat([{"role": "user", "content": "question B"}])
        assert r2.cached is False

    async def test_disabled_cache_never_persists(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ok_response("answer"))

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(base_url="http://fake", transport=transport)
        client = OllamaClient(
            llm_settings(cache={"enabled": False}), http_client=http_client, cache_dir=tmp_path
        )
        await client.chat([{"role": "user", "content": "hi"}])
        assert list(tmp_path.glob("*.json")) == []
