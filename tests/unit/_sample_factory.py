"""Shared TelemetrySample construction for controller tests."""

from __future__ import annotations

from ecoloop.bus.models import SimClock, SiteConditions, TelemetrySample, ZoneTelemetry


def make_zone(
    name: str,
    *,
    occupancy_fraction: float = 1.0,
    air_temperature_c: float = 22.0,
) -> ZoneTelemetry:
    return ZoneTelemetry(
        zone=name,
        air_temperature_c=air_temperature_c,
        relative_humidity_pct=45.0,
        heating_setpoint_c=21.0,
        cooling_setpoint_c=24.0,
        occupancy_fraction=occupancy_fraction,
    )


def make_sample(
    *,
    zones: tuple[ZoneTelemetry, ...],
    outdoor_air_temperature_c: float = 10.0,
    minute: int = 0,
    hour: int = 12,
    timestep_index: int = 1,
) -> TelemetrySample:
    return TelemetrySample(
        clock=SimClock(year=1999, month=6, day=15, hour=hour, minute=minute, day_of_week=3),
        timestep_index=timestep_index,
        environment="environment-3",
        warmup=False,
        site=SiteConditions(
            outdoor_air_temperature_c=outdoor_air_temperature_c,
            outdoor_relative_humidity_pct=50.0,
        ),
        zones=zones,
    )
