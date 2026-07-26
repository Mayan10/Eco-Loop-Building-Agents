"""The cognitive worker: one full reasoning cycle from context to decision.

This is Tier 2 of the two-tier architecture (AGENTS.md §4) — everything here
runs off the main thread, tolerates multi-second latency, and talks to the
simulation only through :mod:`ecoloop.bus`. A cycle assembles context, gives
the model the exact same MCP tools a real client would see, and lets it call
them — through the live :class:`~mcp.server.fastmcp.FastMCP` instance
in-process, not over a transport, since orchestrator and server share this
process — until it either takes a terminal actuate action or exhausts its
per-cycle tool-call budget.

A cycle that calls no actuate tool at all is a **valid** outcome, not a
failure: "hold the current policy" is a real decision (``agent/AGENTS.md``
— degradation is the normal path, not the error path), and the system prompt
says as much.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ecoloop.agent.context import build_context, render_context
from ecoloop.agent.llm import ChatResponse, LLMClient, ToolCall
from ecoloop.agent.prompts import render_system_prompt
from ecoloop.config import AgentSettings
from ecoloop.errors import CircuitOpenError, LLMUnavailableError
from ecoloop.logging import get_logger
from ecoloop.mcp.state import ServerState

__all__ = ["CognitiveOrchestrator"]

_logger = get_logger(__name__, component="agent")

_ACTUATE_TOOL_NAMES = frozenset({"propose_policy", "request_zone_setpoint"})


class CognitiveOrchestrator:
    """Runs one cognitive cycle at a time against a live server and LLM.

    Args:
        state: Live server state (telemetry, policy, zone map).
        server: The in-process MCP server whose tools the model may call.
        llm: The chat client.
        settings: Cadence, tool-call budget, and context settings.
    """

    def __init__(
        self, state: ServerState, server: FastMCP, llm: LLMClient, settings: AgentSettings
    ) -> None:
        """Bind this orchestrator to a state, server, LLM client, and settings."""
        self._state = state
        self._server = server
        self._llm = llm
        self._settings = settings

    async def run_cycle(self) -> str:
        """Run one full reasoning cycle: context, tool calls, decision.

        Returns:
            A short human-readable summary of what happened, for logging —
            e.g. which tools were called and whether an actuate tool fired.
        """
        zone_names = (
            tuple(z.name for z in self._state.zone_map.zones if z.conditioned)
            if self._state.zone_map is not None
            else ()
        )
        blocks = build_context(self._state, self._settings.context)
        context_text = render_context(blocks)

        system_prompt, prompt_version = render_system_prompt(
            context=context_text, zone_names=zone_names
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        tool_schemas = await _tool_schemas(self._server)

        called: list[str] = []
        any_cached = False
        outcome = "no terminal action"
        for _ in range(self._settings.max_tool_calls_per_invocation):
            try:
                response = await self._llm.chat(messages, tools=tool_schemas)
            except (LLMUnavailableError, CircuitOpenError):
                _logger.exception("cognitive cycle aborted: LLM unavailable")
                outcome = "aborted: LLM unavailable"
                self._record_cycle_trace(prompt_version, called, outcome, any_cached)
                return f"aborted (LLM unavailable) after calling: {', '.join(called)}"

            any_cached = any_cached or response.cached
            if not response.tool_calls:
                if response.content:
                    messages.append({"role": "assistant", "content": response.content})
                outcome = "held (no action)"
                break

            terminal = await self._handle_tool_calls(messages, response, called)
            if terminal:
                outcome = "actuated"
                break
        else:
            outcome = "tool-call budget exhausted"
            _logger.warning(
                "cognitive cycle hit its tool-call budget without a terminal action",
                budget=self._settings.max_tool_calls_per_invocation,
            )

        _logger.info("cognitive cycle complete", prompt_version=prompt_version, tools_called=called)
        self._record_cycle_trace(prompt_version, called, outcome, any_cached)
        return f"called: {', '.join(called) if called else '(none)'}"

    def _record_cycle_trace(
        self, prompt_version: str, called: list[str], outcome: str, any_cached: bool
    ) -> None:
        """Record one trace entry summarising the whole cycle's decision.

        This is separate from the per-tool-call entries ``mcp/server.py``
        already writes: those record what each tool returned, this records
        what the *cycle* decided and with which prompt/model, so a decision
        can be traced back to the exact template version and model that
        produced it (AGENTS.md: "the prompt template version, the model name,
        the seed and the prompt hash all go into the trace").

        Args:
            prompt_version: The system prompt template version used.
            called: Tool names called this cycle, in order.
            outcome: A short label for how the cycle ended.
            any_cached: Whether any chat call this cycle was served from cache.
        """
        if self._state.trace is None:
            return
        sample = self._state.telemetry.latest()
        self._state.trace.record(
            tool="cognitive_cycle",
            arguments={
                "prompt_version": prompt_version,
                "model": self._state.settings.llm.model,
                "seed": self._state.settings.llm.seed,
                "temperature": self._state.settings.llm.temperature,
            },
            result_summary=f"outcome={outcome}, tools_called={called}, any_cached={any_cached}",
            sim_clock=sample.clock if sample is not None else None,
        )

    async def _handle_tool_calls(
        self, messages: list[dict[str, Any]], response: ChatResponse, called: list[str]
    ) -> bool:
        """Execute every tool call in a response and append the results.

        Args:
            messages: The running chat history, mutated in place.
            response: The model's response containing tool calls.
            called: Running list of tool names called this cycle, mutated
                in place for logging.

        Returns:
            ``True`` if a terminal actuate tool was called this round.
        """
        messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in response.tool_calls
                ],
            }
        )

        terminal = False
        for call in response.tool_calls:
            called.append(call.name)
            result = await self._call_tool(call)
            messages.append({"role": "tool", "content": result, "tool_call_id": call.id})
            if call.name in _ACTUATE_TOOL_NAMES:
                terminal = True
        return terminal

    async def _call_tool(self, call: ToolCall) -> str:
        """Invoke one MCP tool in-process and render its result as text.

        Args:
            call: The tool call to execute.

        Returns:
            A JSON-ish text rendering of the tool's structured result, or an
            error string if the tool raised (the server's own guard already
            converts exceptions to an "error: ..." string, so this rarely
            raises itself).
        """
        try:
            _content, structured = await self._server.call_tool(call.name, call.arguments)
        except Exception as exc:
            _logger.exception("tool call failed", tool=call.name)
            return f"error: {exc}"
        return str(structured)


async def _tool_schemas(server: FastMCP) -> list[dict[str, Any]]:
    """Convert every registered MCP tool into OpenAI function-calling schema.

    Args:
        server: The MCP server to read tool definitions from.

    Returns:
        Tool schemas in the shape Ollama's ``/api/chat`` ``tools`` parameter
        expects.
    """
    tools = await server.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in tools
    ]
