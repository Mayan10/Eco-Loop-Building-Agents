"""Shared ServerState construction for MCP tool tests."""

from __future__ import annotations

from pathlib import Path

from ecoloop.bus.models import SimClock, SiteConditions, TelemetrySample, ZoneTelemetry
from ecoloop.bus.policy import PolicyStore
from ecoloop.bus.telemetry import TelemetryBus
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.mcp.state import ServerState
from ecoloop.simulation.handles import load_zone_map

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_test_settings() -> EcoLoopSettings:
    return load_settings(profile="fast")


def make_state(**overrides: object) -> ServerState:
    settings = load_test_settings()
    defaults: dict[str, object] = {
        "settings": settings,
        "telemetry": TelemetryBus(capacity=settings.bus.telemetry_capacity),
        "policy": PolicyStore(
            default_ttl_minutes=settings.bus.policy.default_ttl_minutes,
            max_age_minutes=settings.bus.policy.max_age_minutes,
        ),
        "zone_map": load_zone_map(PROJECT_ROOT / "config" / "zones.yaml"),
        "sandbox_roots": settings.mcp.sandbox_roots,
        "trace": None,
    }
    defaults.update(overrides)
    return ServerState(**defaults)  # type: ignore[arg-type]


def make_zone(
    name: str = "CORE_ZN",
    *,
    occupancy_fraction: float = 0.8,
    air_temperature_c: float = 24.0,
    pmv: float | None = 0.2,
    ppd_pct: float | None = 8.0,
    co2_ppm: float | None = 500.0,
) -> ZoneTelemetry:
    return ZoneTelemetry(
        zone=name,
        air_temperature_c=air_temperature_c,
        relative_humidity_pct=45.0,
        heating_setpoint_c=21.0,
        cooling_setpoint_c=24.0,
        occupancy_fraction=occupancy_fraction,
        pmv=pmv,
        ppd_pct=ppd_pct,
        co2_ppm=co2_ppm,
    )


def make_sample(
    *, zones: tuple[ZoneTelemetry, ...], hour: int = 14, minute: int = 0, outdoor_c: float = 28.0
) -> TelemetrySample:
    return TelemetrySample(
        clock=SimClock(year=1999, month=7, day=15, hour=hour, minute=minute, day_of_week=3),
        timestep_index=1,
        environment="environment-3",
        warmup=False,
        site=SiteConditions(
            outdoor_air_temperature_c=outdoor_c, outdoor_relative_humidity_pct=50.0
        ),
        zones=zones,
        meters=(),
    )
