"""Scripted LLM doubles for testing the cognitive layer without a real endpoint.

Each class here satisfies :class:`~ecoloop.agent.llm.OllamaClient`'s duck-typed
``chat()`` interface (``CognitiveOrchestrator`` only ever calls
``await llm.chat(messages, tools=...)``, so anything with that one async
method works). They exist to test the orchestrator's tool-calling loop and
Phase 7's self-healing chaos scenarios against known, reproducible model
behaviour — including the failure modes a real model can exhibit that are
awkward to provoke on demand from a live endpoint.
"""

from __future__ import annotations

from ecoloop.agent.llm import ChatResponse
from ecoloop.errors import LLMUnavailableError, SchemaValidationError

__all__ = ["FailingLLM", "MalformedToolCallLLM", "ScriptedLLM", "TimeoutLLM"]


class ScriptedLLM:
    """Returns each queued :class:`ChatResponse` in order, one per call.

    Args:
        responses: Responses to return, in call order.
    """

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, object]]] = []

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        *,
        model: str | None = None,
    ) -> ChatResponse:
        """Return the next scripted response.

        Args:
            messages: Chat messages (recorded, not used to select a response).
            tools: Tool schemas offered, unused by this fake.
            model: Model override, unused by this fake.

        Returns:
            The next queued response.

        Raises:
            AssertionError: If the script has been exhausted — a test asking
                for more cycles than it scripted is a test bug, not something
                to paper over with a default response.
        """
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        return self._responses.pop(0)


class FailingLLM:
    """Always raises, simulating an endpoint that is completely unreachable."""

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        *,
        model: str | None = None,
    ) -> ChatResponse:
        """Always raise LLMUnavailableError.

        Raises:
            LLMUnavailableError: Unconditionally.
        """
        raise LLMUnavailableError("simulated endpoint outage")


class TimeoutLLM:
    """Always raises, simulating a request that times out rather than erroring cleanly."""

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        *,
        model: str | None = None,
    ) -> ChatResponse:
        """Always raise LLMUnavailableError with a timeout-flavoured message.

        Raises:
            LLMUnavailableError: Unconditionally.
        """
        raise LLMUnavailableError("simulated request timeout")


class MalformedToolCallLLM:
    """Always raises, simulating a response that fails schema validation.

    Models occasionally emit a tool call with the wrong argument shape (a
    string where a number was expected, a missing required field). This
    stands in for that failure mode without needing to coax a real model
    into producing it on demand.
    """

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        *,
        model: str | None = None,
    ) -> ChatResponse:
        """Always raise SchemaValidationError.

        Raises:
            SchemaValidationError: Unconditionally.
        """
        raise SchemaValidationError("simulated malformed tool call arguments")
