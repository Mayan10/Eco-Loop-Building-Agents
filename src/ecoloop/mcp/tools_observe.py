"""Read-only MCP tools: the LLM's view into the running building.

None of these mutate anything — they only read from
:class:`~ecoloop.mcp.state.ServerState`'s telemetry bus, policy store, or
static configuration. Every one degrades gracefully to an empty/"no data"
result when no simulation has published telemetry yet, rather than raising:
a cold MCP server with nothing attached is a normal state, not an error.
"""

from __future__ import annotations

from ecoloop.bus.models import ZoneTelemetry
from ecoloop.mcp.models import (
    ComfortStatusResult,
    DemandStatusResult,
    EnergyTotalsResult,
    ForecastHourResult,
    SignalHourResult,
    SiteConditionsResult,
    ZoneComfortResult,
    ZoneTelemetryResult,
)
from ecoloop.mcp.signals import read_carbon_intensity, read_tariff
from ecoloop.mcp.state import ServerState

__all__ = [
    "get_carbon_intensity",
    "get_comfort_status",
    "get_demand_status",
    "get_energy_totals",
    "get_site_conditions",
    "get_tariff",
    "get_weather_forecast",
    "get_zone_telemetry",
]


def _zone_result(z: ZoneTelemetry) -> ZoneTelemetryResult:
    """Build a ZoneTelemetryResult from a bus ZoneTelemetry.

    Args:
        z: The zone's current telemetry.

    Returns:
        The corresponding MCP result model.
    """
    return ZoneTelemetryResult(
        zone=z.zone,
        air_temperature_c=z.air_temperature_c,
        relative_humidity_pct=z.relative_humidity_pct,
        heating_setpoint_c=z.heating_setpoint_c,
        cooling_setpoint_c=z.cooling_setpoint_c,
        occupancy_fraction=z.occupancy_fraction,
        pmv=z.pmv,
        ppd_pct=z.ppd_pct,
        co2_ppm=z.co2_ppm,
    )


def get_zone_telemetry(
    state: ServerState, zone: str | None = None
) -> tuple[ZoneTelemetryResult, ...]:
    """Current temperature, humidity, setpoints, and comfort for one or all zones.

    Args:
        state: Server state.
        zone: A specific zone name, case-insensitive. All zones if omitted.

    Returns:
        Matching zones' current telemetry. Empty if no sample has been
        published yet, or the named zone does not exist.
    """
    sample = state.telemetry.latest()
    if sample is None:
        return ()
    if zone is None:
        return tuple(_zone_result(z) for z in sample.zones)
    found = sample.zone(zone)
    return (_zone_result(found),) if found is not None else ()


def get_site_conditions(state: ServerState) -> SiteConditionsResult | None:
    """Current outdoor temperature, humidity, radiation, and wind.

    Args:
        state: Server state.

    Returns:
        The latest site conditions, or ``None`` if no sample has published yet.
    """
    sample = state.telemetry.latest()
    if sample is None:
        return None
    return SiteConditionsResult(
        outdoor_air_temperature_c=sample.site.outdoor_air_temperature_c,
        outdoor_relative_humidity_pct=sample.site.outdoor_relative_humidity_pct,
        direct_normal_radiation_w_m2=sample.site.direct_normal_radiation_w_m2,
        diffuse_horizontal_radiation_w_m2=sample.site.diffuse_horizontal_radiation_w_m2,
        wind_speed_m_s=sample.site.wind_speed_m_s,
        sim_clock_iso=sample.clock.isoformat(),
    )


def get_comfort_status(state: ServerState) -> ComfortStatusResult:
    """Building-wide ASHRAE 55 compliance, and the worst comfort offender.

    Args:
        state: Server state.

    Returns:
        Per-zone PMV/PPD compliance and the zone furthest from neutral.
    """
    sample = state.telemetry.latest()
    if sample is None:
        return ComfortStatusResult(
            zones=(), worst_zone=None, worst_abs_pmv=None, any_samples_available=False
        )

    comfort = state.settings.comfort
    zone_results: list[ZoneComfortResult] = []
    worst_zone: str | None = None
    worst_abs_pmv: float | None = None

    for z in sample.zones:
        within: bool | None = None
        if z.pmv is not None:
            within = comfort.pmv_occupied_min <= z.pmv <= comfort.pmv_occupied_max
            if worst_abs_pmv is None or abs(z.pmv) > worst_abs_pmv:
                worst_abs_pmv = abs(z.pmv)
                worst_zone = z.zone
        zone_results.append(
            ZoneComfortResult(zone=z.zone, pmv=z.pmv, ppd_pct=z.ppd_pct, within_ashrae_55=within)
        )

    return ComfortStatusResult(
        zones=tuple(zone_results),
        worst_zone=worst_zone,
        worst_abs_pmv=worst_abs_pmv,
        any_samples_available=True,
    )


def get_energy_totals(
    state: ServerState, window_minutes: float | None = None
) -> EnergyTotalsResult:
    """Meter totals over a trailing window, converted to kWh.

    Args:
        state: Server state.
        window_minutes: Trailing window length, in simulation time. Defaults
            to ``bus.aggregate_window_minutes`` if omitted.

    Returns:
        Facility-level energy totals, plus a per-meter breakdown.
    """
    effective_window = window_minutes or state.settings.bus.aggregate_window_minutes
    samples = state.telemetry.window(effective_window)
    joules_per_kwh = state.settings.analysis.joules_per_kwh

    by_meter_joules: dict[str, float] = {}
    for sample in samples:
        for meter in sample.meters:
            by_meter_joules[meter.name] = by_meter_joules.get(meter.name, 0.0) + meter.joules

    return EnergyTotalsResult(
        window_minutes=effective_window,
        samples_in_window=len(samples),
        total_kwh=sum(s.total_site_joules for s in samples) / joules_per_kwh,
        by_meter_kwh={name: joules / joules_per_kwh for name, joules in by_meter_joules.items()},
    )


def get_carbon_intensity(state: ServerState, hours_ahead: int = 0) -> SignalHourResult:
    """Grid carbon intensity for the current hour, or an hour ahead of it.

    Args:
        state: Server state.
        hours_ahead: Hours ahead of the latest sample's clock (0 = current hour).

    Returns:
        The carbon intensity for that hour of day.
    """
    hour = _hour_of_day(state, hours_ahead)
    path = state.settings.resolve(state.settings.signals.carbon_intensity_csv)
    return SignalHourResult(
        hour_of_day=hour, value=read_carbon_intensity(path, hour), unit="gCO2/kWh"
    )


def get_tariff(state: ServerState, hours_ahead: int = 0) -> SignalHourResult:
    """Time-of-use energy price for the current hour, or an hour ahead of it.

    Args:
        state: Server state.
        hours_ahead: Hours ahead of the latest sample's clock (0 = current hour).

    Returns:
        The tariff for that hour of day.
    """
    hour = _hour_of_day(state, hours_ahead)
    path = state.settings.resolve(state.settings.signals.tariff_csv)
    return SignalHourResult(hour_of_day=hour, value=read_tariff(path, hour), unit="currency/kWh")


def _hour_of_day(state: ServerState, hours_ahead: int) -> int:
    """The latest sample's hour of day, shifted forward and wrapped to 0-23.

    Args:
        state: Server state.
        hours_ahead: Hours to shift forward.

    Returns:
        Hour of day, 0-23. Defaults to hour 0 if no sample is available yet.
    """
    sample = state.telemetry.latest()
    base_hour = sample.clock.hour if sample is not None else 0
    return (base_hour + hours_ahead) % 24


def get_weather_forecast(state: ServerState, hours_ahead: int) -> tuple[ForecastHourResult, ...]:
    """Read ahead in the weather file — the disclosed forecast oracle.

    See ``docs/ARCHITECTURE.md``: this looks past the simulation's current
    position, a capability only the cognitive layer is meant to use.

    Args:
        state: Server state.
        hours_ahead: How many hours ahead to forecast, capped at
            ``simulation.output.max_forecast_horizon_hours``.

    Returns:
        Forecast hours in chronological order, starting at the current hour.
        Empty if no weather file or no active sample is available.
    """
    sample = state.telemetry.latest()
    if sample is None or state.weather is None:
        return ()
    capped_hours = min(hours_ahead, state.settings.simulation.output.max_forecast_horizon_hours)
    epw_hour = sample.clock.hour + 1  # EPW uses a 1-24 hour convention
    forecast = state.weather.forecast(
        sample.clock.month, sample.clock.day, epw_hour, horizon_hours=capped_hours
    )
    return tuple(
        ForecastHourResult(
            month=hour.month,
            day=hour.day,
            hour=hour.hour,
            dry_bulb_c=hour.dry_bulb_c,
            wind_speed_m_s=hour.wind_speed_m_s,
        )
        for hour in forecast
    )


def get_demand_status(state: ServerState) -> DemandStatusResult:
    """Rolling electrical demand against the configured peak-shaving cap.

    Args:
        state: Server state.

    Returns:
        The rolling average demand over ``control.demand_window_minutes``,
        as a fraction of ``control.demand_cap_kw``.
    """
    control = state.settings.control
    samples = state.telemetry.window(control.demand_window_minutes)
    joules_per_kwh = state.settings.analysis.joules_per_kwh

    electricity_joules = sum(s.meter("ElectricityNet:Facility") for s in samples)
    window_hours = control.demand_window_minutes / 60.0
    rolling_kw = (electricity_joules / joules_per_kwh) / window_hours if window_hours > 0 else 0.0
    fraction = rolling_kw / control.demand_cap_kw if control.demand_cap_kw > 0 else 0.0

    return DemandStatusResult(
        window_minutes=control.demand_window_minutes,
        rolling_average_kw=rolling_kw,
        demand_cap_kw=control.demand_cap_kw,
        fraction_of_cap=fraction,
        approaching_cap=fraction >= control.demand_trigger_fraction,
    )
