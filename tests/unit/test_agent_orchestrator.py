"""Tests for CognitiveOrchestrator's tool-calling loop.

Uses a scripted fake LLM client (no real Ollama call) against the real,
in-process FastMCP server - so the tool-execution half of the loop is
genuine, only the model's responses are canned.
"""

from __future__ import annotations

import json
from pathlib import Path

from _mcp_state_factory import make_sample, make_state, make_zone
from fake_llm import FailingLLM, ScriptedLLM

from ecoloop.agent.llm import ChatResponse, ToolCall
from ecoloop.agent.orchestrator import CognitiveOrchestrator
from ecoloop.config import AgentSettings, ContextSettings
from ecoloop.mcp.server import build_server
from ecoloop.mcp.trace import TraceWriter


def agent_settings(**overrides: object) -> AgentSettings:
    defaults: dict[str, object] = {
        "cadence_minutes": 60.0,
        "min_invocation_gap_minutes": 15.0,
        "max_tool_calls_per_invocation": 4,
        "context": ContextSettings(
            max_input_tokens=4000,
            max_output_tokens=800,
            block_priority=("zone_summary", "comfort_status"),
            max_tool_result_tokens=700,
            chars_per_token=3.6,
        ),
    }
    defaults.update(overrides)
    return AgentSettings(**defaults)  # type: ignore[arg-type]


def state_with_sample():
    state = make_state()
    state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN", pmv=0.9),)))
    return state


class TestNoActionTaken:
    async def test_final_text_with_no_tool_calls_ends_the_cycle(self) -> None:
        state = state_with_sample()
        server = build_server(state)
        llm = ScriptedLLM([ChatResponse(content="Nothing needs to change.", tool_calls=())])
        orchestrator = CognitiveOrchestrator(state, server, llm, agent_settings())

        summary = await orchestrator.run_cycle()

        assert summary == "called: (none)"
        assert state.policy.current(state.telemetry.latest().clock) is None


class TestTerminalActuation:
    async def test_observe_then_actuate_publishes_a_policy(self) -> None:
        state = state_with_sample()
        server = build_server(state)
        llm = ScriptedLLM(
            [
                ChatResponse(
                    content="",
                    tool_calls=(
                        ToolCall(id="1", name="get_zone_telemetry", arguments={"zone": "CORE_ZN"}),
                    ),
                ),
                ChatResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            id="2",
                            name="request_zone_setpoint",
                            arguments={
                                "zone": "CORE_ZN",
                                "reasoning": "too warm",
                                "cooling_c": 22.0,
                            },
                        ),
                    ),
                ),
            ]
        )
        orchestrator = CognitiveOrchestrator(state, server, llm, agent_settings())

        summary = await orchestrator.run_cycle()

        assert "get_zone_telemetry" in summary
        assert "request_zone_setpoint" in summary
        published = state.policy.current(state.telemetry.latest().clock)
        assert published is not None
        assert published.zone("CORE_ZN").cooling_setpoint_c == 22.0

    async def test_stops_immediately_after_the_terminal_tool_even_with_budget_left(self) -> None:
        state = state_with_sample()
        server = build_server(state)
        llm = ScriptedLLM(
            [
                ChatResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            id="1",
                            name="request_zone_setpoint",
                            arguments={"zone": "CORE_ZN", "reasoning": "test", "cooling_c": 23.0},
                        ),
                    ),
                ),
            ]
        )
        orchestrator = CognitiveOrchestrator(
            state, server, llm, agent_settings(max_tool_calls_per_invocation=10)
        )

        await orchestrator.run_cycle()

        # Only one .chat() call should have happened - the loop must not ask
        # for a second round after a terminal action.
        assert len(llm.calls) == 1


class TestBudgetExhaustion:
    async def test_hitting_the_tool_call_budget_without_a_terminal_action_stops_cleanly(
        self,
    ) -> None:
        state = state_with_sample()
        server = build_server(state)
        non_terminal = ChatResponse(
            content="",
            tool_calls=(ToolCall(id="x", name="get_zone_telemetry", arguments={}),),
        )
        llm = ScriptedLLM([non_terminal, non_terminal])
        orchestrator = CognitiveOrchestrator(
            state, server, llm, agent_settings(max_tool_calls_per_invocation=2)
        )

        summary = await orchestrator.run_cycle()

        assert state.policy.current(state.telemetry.latest().clock) is None
        assert "get_zone_telemetry" in summary


class TestLLMFailure:
    async def test_unavailable_llm_aborts_the_cycle_without_raising(self) -> None:
        state = state_with_sample()
        server = build_server(state)
        orchestrator = CognitiveOrchestrator(state, server, FailingLLM(), agent_settings())

        summary = await orchestrator.run_cycle()

        assert "aborted" in summary


class TestCycleTrace:
    """The orchestrator must record its own decision - prompt version, model,
    seed - separately from the per-tool-call entries mcp/server.py writes."""

    async def test_records_a_cognitive_cycle_entry(self, tmp_path: Path) -> None:
        state = state_with_sample()
        state.trace = TraceWriter(tmp_path / "trace.jsonl", max_bytes=1_000_000)
        server = build_server(state)
        llm = ScriptedLLM([ChatResponse(content="Nothing needs to change.", tool_calls=())])
        orchestrator = CognitiveOrchestrator(state, server, llm, agent_settings())

        await orchestrator.run_cycle()

        lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        cycle_entries = [
            json.loads(line) for line in lines if json.loads(line)["tool"] == "cognitive_cycle"
        ]
        assert len(cycle_entries) == 1
        entry = cycle_entries[0]
        assert entry["arguments"]["prompt_version"] == "v1"
        assert entry["arguments"]["model"] == state.settings.llm.model
        assert entry["arguments"]["seed"] == state.settings.llm.seed
        assert "held" in entry["result_summary"]

    async def test_aborted_cycle_still_records_a_trace_entry(self, tmp_path: Path) -> None:
        state = state_with_sample()
        state.trace = TraceWriter(tmp_path / "trace.jsonl", max_bytes=1_000_000)
        server = build_server(state)
        orchestrator = CognitiveOrchestrator(state, server, FailingLLM(), agent_settings())

        await orchestrator.run_cycle()

        lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        entries = [
            json.loads(line) for line in lines if json.loads(line)["tool"] == "cognitive_cycle"
        ]
        assert len(entries) == 1
        assert "aborted" in entries[0]["result_summary"]
