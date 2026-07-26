"""Drive the cognitive tier on its own cadence, on a dedicated worker thread.

This is the piece AGENTS.md's landmines call "the worker-thread wiring — a
distinct integration task": every component it combines
(:class:`~ecoloop.agent.orchestrator.CognitiveOrchestrator`, the in-process
MCP server, an :class:`~ecoloop.agent.llm.OllamaClient`) already existed and
was already proven correct against a real running Ollama server — but only
ever invoked directly from a test, never from its own thread running
alongside a live, main-thread-blocking EnergyPlus run.

**Cadence is measured in simulated minutes, not wall-clock seconds.**
EnergyPlus's speed relative to real time varies enormously with profile and
machine — the fast profile's two simulated weeks were observed completing in
under two seconds of real time in this repository, and an annual run can take
minutes to hours. Gating on wall-clock time would invoke the model far too
often on a fast run and not often enough on a slow one; gating on the
simulation's own clock (carried by every :class:`~ecoloop.bus.models.TelemetrySample`)
gives the same cognitive cadence regardless of how fast the physics solver
happens to run.

**Never touches the EnergyPlus API.** This thread only reads
:class:`~ecoloop.bus.telemetry.TelemetryBus` (thread-safe by construction,
see its docstring) and calls into the orchestrator, which itself only calls
MCP tools reading/writing the same thread-safe bus objects. Calling the
EnergyPlus API from this thread would segfault the C++ runtime with no
traceback (AGENTS.md invariant #7) — nothing here is capable of that by
construction, since this module has no import of ``simulation/``.
"""

from __future__ import annotations

import asyncio
import threading

from mcp.server.fastmcp import FastMCP

from ecoloop.agent.llm import LLMClient
from ecoloop.agent.orchestrator import CognitiveOrchestrator
from ecoloop.bus.models import SimClock
from ecoloop.config import AgentSettings
from ecoloop.logging import get_logger
from ecoloop.mcp.state import ServerState

__all__ = ["CognitiveWorker"]

_logger = get_logger(__name__, component="agent")

_POLL_INTERVAL_SECONDS = 0.05


class CognitiveWorker:
    """Runs :meth:`CognitiveOrchestrator.run_cycle` on a cadence, off-thread.

    Args:
        state: Live server state, shared with the main thread's reflex tier
            and EnergyPlus callbacks.
        server: The in-process MCP server the orchestrator calls as a client.
        llm: The chat client.
        settings: Cadence and tool-call budget settings (``agent.*``).
    """

    def __init__(
        self, state: ServerState, server: FastMCP, llm: LLMClient, settings: AgentSettings
    ) -> None:
        """Bind this worker to a state, server, LLM client, and settings."""
        self._state = state
        self._settings = settings
        self._orchestrator = CognitiveOrchestrator(state, server, llm, settings)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_invocation_clock: SimClock | None = None
        self._cycles_run = 0

    @property
    def cycles_run(self) -> int:
        """How many cognitive cycles have completed so far."""
        return self._cycles_run

    def start(self) -> None:
        """Start the worker thread. Must be called before EnergyPlus's ``run()``."""
        self._thread = threading.Thread(
            target=self._run_forever, name="ecoloop-cognitive-worker", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 30.0) -> None:
        """Signal the worker to stop and wait for it to finish its current cycle.

        Args:
            timeout_seconds: How long to wait before giving up. The thread is
                a daemon, so the process can still exit even if this times
                out — but a timeout is logged, since it means a cognitive
                cycle (bounded by ``agent.max_tool_calls_per_invocation`` and
                the LLM's own timeout) took unexpectedly long.
        """
        self._stop_event.set()
        if self._thread is None:
            return
        self._thread.join(timeout=timeout_seconds)
        if self._thread.is_alive():
            _logger.warning(
                "cognitive worker did not stop within timeout", timeout_seconds=timeout_seconds
            )

    def _run_forever(self) -> None:
        """Thread entry point: owns its own asyncio event loop."""
        try:
            asyncio.run(self._loop())
        except Exception:
            _logger.exception("cognitive worker thread crashed")

    async def _loop(self) -> None:
        """Poll for new telemetry and invoke a cognitive cycle when due."""
        while not self._stop_event.is_set():
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            sample = self._state.telemetry.latest()
            if sample is None or sample.warmup or not self._due(sample.clock):
                continue
            self._last_invocation_clock = sample.clock
            try:
                summary = await self._orchestrator.run_cycle()
                self._cycles_run += 1
                _logger.info("cognitive cycle complete", summary=summary, cycle=self._cycles_run)
            except Exception:
                # run_cycle() is documented to never raise - any exception
                # reaching here is a bug in that contract, not a reason to
                # kill the thread mid-run.
                _logger.exception("cognitive cycle raised unexpectedly")

    def _due(self, clock: SimClock) -> bool:
        """Whether enough simulated time has passed since the last invocation."""
        if self._last_invocation_clock is None:
            return True
        elapsed_minutes = (
            clock.as_datetime - self._last_invocation_clock.as_datetime
        ).total_seconds() / 60.0
        if elapsed_minutes < 0:
            # An environment/year boundary makes the clock look like it went
            # backwards; treat that the same as "never invoked yet".
            return True
        return elapsed_minutes >= self._settings.cadence_minutes
