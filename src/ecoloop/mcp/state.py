"""The live objects every MCP tool reads from or writes to.

One ``ServerState`` per server process, threaded into every tool via closure
in :mod:`ecoloop.mcp.server`. Tools never construct their own bus references
— that would make "which telemetry bus is this tool reading" an open
question instead of a fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ecoloop.bus.policy import PolicyStore
from ecoloop.bus.telemetry import TelemetryBus
from ecoloop.config import EcoLoopSettings
from ecoloop.control.reflex import ReflexController
from ecoloop.logging import get_logger
from ecoloop.mcp.trace import TraceWriter
from ecoloop.simulation.handles import ZoneMap, load_zone_map
from ecoloop.simulation.weather import WeatherFile, load_epw

__all__ = ["ServerState", "create_standalone_state"]

_logger = get_logger(__name__, component="mcp")


@dataclass
class ServerState:
    """Everything an MCP tool needs, bundled for a single server instance.

    Args:
        settings: Loaded Eco-Loop settings.
        telemetry: The telemetry bus a live simulation publishes into. Empty
            (never published to) when no simulation is attached.
        policy: The policy store agent-sourced proposals are published to.
        zone_map: The parsed ``config/zones.yaml``, for validating zone names
            in proposals and describing the building's shape.
        reflex: The reflex controller, if one is attached to a live run —
            needed for ``get_guardrail_violations``.
        weather: The parsed EPW, for the forecast oracle tool.
        err_path: Path to the active run's ``.err`` file, if any.
        trace: Where every tool call gets recorded.
    """

    settings: EcoLoopSettings
    telemetry: TelemetryBus
    policy: PolicyStore
    zone_map: ZoneMap | None = None
    reflex: ReflexController | None = None
    weather: WeatherFile | None = None
    err_path: Path | None = None
    trace: TraceWriter | None = None
    sandbox_roots: tuple[Path, ...] = field(default_factory=tuple)


def create_standalone_state(settings: EcoLoopSettings) -> ServerState:
    """Build server state for ``ecoloop mcp serve`` with no attached simulation.

    Telemetry and policy start empty — every observe tool reports "no data
    available" until a simulation actually publishes into these same bus
    objects. Zone map and weather load eagerly since they are static,
    file-backed metadata useful even without a live run.

    Args:
        settings: Loaded Eco-Loop settings.

    Returns:
        A fresh, unattached server state.
    """
    telemetry = TelemetryBus(capacity=settings.bus.telemetry_capacity)
    policy = PolicyStore(
        default_ttl_minutes=settings.bus.policy.default_ttl_minutes,
        max_age_minutes=settings.bus.policy.max_age_minutes,
    )

    zone_map: ZoneMap | None = None
    try:
        zone_map = load_zone_map(settings.resolve(Path("config/zones.yaml")))
    except Exception:
        _logger.exception("could not load zone map for standalone MCP server")

    weather: WeatherFile | None = None
    try:
        weather = load_epw(settings.resolve(settings.simulation.weather))
    except Exception:
        _logger.exception("no weather file available for standalone MCP server")

    trace = TraceWriter(
        settings.resolve(Path("results/agent_trace.jsonl")),
        max_bytes=settings.logging.max_trace_mib * 1024 * 1024,
    )

    return ServerState(
        settings=settings,
        telemetry=telemetry,
        policy=policy,
        zone_map=zone_map,
        weather=weather,
        trace=trace,
        sandbox_roots=settings.mcp.sandbox_roots,
    )
