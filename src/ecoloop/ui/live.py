"""Rich live terminal view for ``ecoloop run agent --live``.

A third daemon thread, alongside the EnergyPlus main thread and the
:class:`~ecoloop.agent.worker.CognitiveWorker` thread — it only ever reads
:class:`~ecoloop.bus.telemetry.TelemetryBus` (thread-safe by construction)
and :attr:`~ecoloop.agent.worker.CognitiveWorker.cycles_run`, never touches
the EnergyPlus API, and writes nothing any other thread reads. Coverage is
intentionally not required for this module (``pyproject.toml``'s
``[tool.coverage.run] omit`` excludes ``ui/`` and ``dashboard/`` — a
terminal renderer isn't meaningfully unit-testable); it was verified by
hand against a real run instead.
"""

from __future__ import annotations

import threading
import time

from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from ecoloop.agent.worker import CognitiveWorker
from ecoloop.mcp.state import ServerState

__all__ = ["LiveDashboard"]

_REFRESH_SECONDS = 0.5


class LiveDashboard:
    """Renders a live-updating terminal view of a running agent-controller simulation.

    Args:
        state: Live server state, shared with the reflex tier and the
            cognitive worker.
        worker: The cognitive worker whose cycle count this view reports.
        title: Panel title, conventionally ``analysis.report.title``.
    """

    def __init__(self, state: ServerState, worker: CognitiveWorker, *, title: str) -> None:
        """Bind this dashboard to a state, worker, and display title."""
        self._state = state
        self._worker = worker
        self._title = title
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = time.monotonic()

    def start(self) -> None:
        """Start rendering. Must be called before EnergyPlus's ``run()``."""
        self._thread = threading.Thread(
            target=self._run, name="ecoloop-live-dashboard", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop rendering, leaving the final frame on screen."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        with Live(self._render(), refresh_per_second=4, transient=False) as live:
            while not self._stop_event.wait(_REFRESH_SECONDS):
                live.update(self._render())
            live.update(self._render())

    def _render(self) -> RenderableType:
        sample = self._state.telemetry.latest()
        elapsed_seconds = time.monotonic() - self._started_at

        header = Table.grid(padding=(0, 2))
        header.add_column(style="bold")
        header.add_column()
        header.add_row("Simulated time", sample.clock.isoformat() if sample else "warming up…")
        header.add_row("Wall-clock elapsed", f"{elapsed_seconds:.0f}s")
        header.add_row("Cognitive cycles run", str(self._worker.cycles_run))

        zones = Table(title="Zones", show_header=True, header_style="bold")
        zones.add_column("Zone")
        zones.add_column("Temp °C", justify="right")
        zones.add_column("PMV", justify="right")
        zones.add_column("Occupied", justify="right")
        threshold = self._state.settings.comfort.occupied_threshold_fraction
        for zone in sample.zones if sample is not None else ():
            zones.add_row(
                zone.zone,
                f"{zone.air_temperature_c:.1f}",
                f"{zone.pmv:.2f}" if zone.pmv is not None else "n/a",
                "yes" if zone.occupancy_fraction > threshold else "no",
            )

        return Panel(Group(header, zones), title=self._title, border_style="green")
