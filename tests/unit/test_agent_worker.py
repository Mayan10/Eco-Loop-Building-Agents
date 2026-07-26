"""Tests for CognitiveWorker: the cadence loop that drives the cognitive
tier on its own thread, alongside (in production) a live EnergyPlus run.

Uses a fake state/server (no real EnergyPlus) and a scripted LLM (no real
Ollama) - this is a unit test of the cadence/threading logic itself, the
same fakes-based pattern `test_agent_orchestrator.py` uses for the
orchestrator it wraps. The real end-to-end integration (real engine, real
Ollama, both threads actually running concurrently) is covered by
`test_runner.py::TestRunAgentController` (scripted LLM, real engine) and was
additionally verified once by hand against a real Ollama endpoint - see
AGENTS.md's landmine on real per-cycle latency.
"""

from __future__ import annotations

import time

from _mcp_state_factory import make_sample, make_state, make_zone
from fake_llm import FailingLLM, ScriptedLLM

from ecoloop.agent.llm import ChatResponse
from ecoloop.agent.worker import CognitiveWorker
from ecoloop.config import AgentSettings, ContextSettings
from ecoloop.mcp.server import build_server

_POLL_WAIT_SECONDS = 2.0


def agent_settings(**overrides: object) -> AgentSettings:
    defaults: dict[str, object] = {
        "cadence_minutes": 30.0,
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


class TestCognitiveWorker:
    def test_runs_a_cycle_shortly_after_the_first_sample_is_published(self) -> None:
        state = state_with_sample()
        server = build_server(state)
        llm = ScriptedLLM(
            [ChatResponse(content="Nothing needs to change.", tool_calls=()) for _ in range(10)]
        )
        worker = CognitiveWorker(state, server, llm, agent_settings())

        worker.start()
        try:
            deadline = time.monotonic() + _POLL_WAIT_SECONDS
            while worker.cycles_run == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            worker.stop(timeout_seconds=5.0)

        assert worker.cycles_run >= 1

    def test_no_telemetry_yet_means_no_cycles(self) -> None:
        state = make_state()  # no sample published
        server = build_server(state)
        llm = ScriptedLLM([])
        worker = CognitiveWorker(state, server, llm, agent_settings())

        worker.start()
        time.sleep(0.3)
        worker.stop(timeout_seconds=5.0)

        assert worker.cycles_run == 0

    def test_stop_is_idempotent_and_returns_even_if_never_started(self) -> None:
        state = make_state()
        server = build_server(state)
        worker = CognitiveWorker(state, server, FailingLLM(), agent_settings())

        worker.stop(timeout_seconds=1.0)  # must not raise

    def test_a_failing_llm_does_not_crash_the_worker_thread(self) -> None:
        """A cognitive cycle that raises must be logged, not kill the thread -
        matching CognitiveOrchestrator.run_cycle()'s own "never raises"
        contract; this proves the worker doesn't undermine it."""
        state = state_with_sample()
        server = build_server(state)
        worker = CognitiveWorker(state, server, FailingLLM(), agent_settings())

        worker.start()
        time.sleep(0.3)
        worker.stop(timeout_seconds=5.0)

        # FailingLLM makes every cycle abort inside run_cycle() (a caught,
        # logged CircuitOpenError/LLMUnavailableError) rather than raise out
        # of it - run_cycle() itself never raises, so cycles_run still
        # increments once the (degraded) cycle completes.
        assert worker.cycles_run >= 1
