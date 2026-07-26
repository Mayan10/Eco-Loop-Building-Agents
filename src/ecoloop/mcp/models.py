"""Pydantic schemas for every MCP tool's input and output.

AGENTS.md §8: Pydantic v2 at every boundary. The MCP tool surface is the
LLM's *only* actuation channel (AGENTS.md invariant #3), so every argument
and every return value is a typed, validated model — never a bare dict
crossing this boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ActivePolicyResult",
    "ComfortStatusResult",
    "DemandStatusResult",
    "EnergyTotalsResult",
    "ErrorRecordResult",
    "ForecastHourResult",
    "GuardrailViolationResult",
    "PolicyProposalResult",
    "RunManifestResult",
    "SignalHourResult",
    "SiteConditionsResult",
    "ZoneComfortResult",
    "ZoneSetpointProposal",
    "ZoneTelemetryResult",
]


class _Result(BaseModel):
    """Base for every tool return value: strict, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ZoneTelemetryResult(_Result):
    """Current state of one zone."""

    zone: str
    air_temperature_c: float
    relative_humidity_pct: float
    heating_setpoint_c: float
    cooling_setpoint_c: float
    occupancy_fraction: float
    pmv: float | None
    ppd_pct: float | None
    co2_ppm: float | None


class SiteConditionsResult(_Result):
    """Current outdoor conditions."""

    outdoor_air_temperature_c: float
    outdoor_relative_humidity_pct: float
    direct_normal_radiation_w_m2: float
    diffuse_horizontal_radiation_w_m2: float
    wind_speed_m_s: float
    sim_clock_iso: str | None


class ZoneComfortResult(_Result):
    """One zone's ASHRAE 55 compliance at the latest sample."""

    zone: str
    pmv: float | None
    ppd_pct: float | None
    within_ashrae_55: bool | None
    """``None`` when PMV is unavailable for this zone (no Fanger model)."""


class ComfortStatusResult(_Result):
    """Building-wide comfort summary."""

    zones: tuple[ZoneComfortResult, ...]
    worst_zone: str | None
    worst_abs_pmv: float | None
    any_samples_available: bool


class EnergyTotalsResult(_Result):
    """Meter totals over a trailing window, converted to kWh."""

    window_minutes: float
    samples_in_window: int
    total_kwh: float
    by_meter_kwh: dict[str, float]


class ZoneSetpointProposal(BaseModel):
    """One zone's proposed heating/cooling setpoints, as an MCP tool argument.

    Untrusted input: whatever the LLM proposes here is clamped through
    ``control.guardrails`` in the reflex tier before it ever reaches an
    actuator (AGENTS.md invariant #2). This model only validates *shape* —
    that a zone name and two numbers were supplied — never safety.
    """

    model_config = ConfigDict(extra="forbid")

    zone: str
    heating_setpoint_c: float
    cooling_setpoint_c: float


class PolicyProposalResult(_Result):
    """Outcome of a ``propose_policy`` / ``request_zone_setpoint`` call."""

    accepted: bool
    policy_id: str | None
    rejected_zones: tuple[str, ...]
    """Zone names in the proposal that do not exist in the active zone map."""
    message: str


class ActivePolicyResult(_Result):
    """The currently active policy, if any."""

    has_active_policy: bool
    source: str | None
    age_minutes: float | None
    ttl_minutes: float | None
    reasoning: str | None
    zone_setpoints: dict[str, tuple[float, float]]


class ErrorRecordResult(_Result):
    """One EnergyPlus ``.err`` record."""

    severity: str
    message: str
    line_number: int


class RunManifestResult(_Result):
    """Run-level metadata for self-orientation."""

    profile: str
    controller: str
    published_samples: int
    dropped_samples: int
    zones_conditioned: tuple[str, ...]
    run_period: str


class SignalHourResult(_Result):
    """One hour's value from a grid signal (carbon or tariff)."""

    hour_of_day: int
    value: float
    unit: str


class ForecastHourResult(_Result):
    """One hour of the disclosed weather forecast oracle.

    See ``docs/ARCHITECTURE.md``: this reads ahead of the simulation's
    current position in the EPW file, which only the cognitive layer is
    meant to use.
    """

    month: int
    day: int
    hour: int
    dry_bulb_c: float | None
    wind_speed_m_s: float | None


class GuardrailViolationResult(_Result):
    """One recorded guardrail intervention."""

    sim_clock_iso: str
    zone: str
    violations: tuple[str, ...]


class DemandStatusResult(_Result):
    """Rolling electrical demand against the configured cap."""

    window_minutes: float
    rolling_average_kw: float
    demand_cap_kw: float
    fraction_of_cap: float
    approaching_cap: bool
