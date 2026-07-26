"""Typed payloads that cross the thread boundary.

``bus/`` is the innermost layer: it imports nothing from any other Eco-Loop
package, which is what lets both the simulation thread and the cognitive worker
depend on it without creating a cycle. ``import-linter`` enforces that.

Everything here is a **frozen** Pydantic model. Immutability is not stylistic —
:class:`TelemetrySample` is published by the EnergyPlus callback and read
concurrently by the worker thread, and a frozen object cannot be observed
half-updated. The same reasoning governs ``ControlPolicy`` in
:mod:`ecoloop.bus.policy`.

Energy values are carried in **Joules**, exactly as EnergyPlus reports them. No
model here converts to kWh. That conversion needs ``analysis.joules_per_kwh``
from configuration, and doing it in two places is how a project ends up with two
different answers for the same run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MeterReading",
    "SimClock",
    "SiteConditions",
    "TelemetrySample",
    "ZoneTelemetry",
]

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Percent = Annotated[float, Field(ge=0.0, le=100.0)]


class _Frozen(BaseModel):
    """Base for every bus payload: immutable and strict about unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SimClock(_Frozen):
    """Simulation calendar position at the moment a sample was taken.

    EnergyPlus exposes the date and time piecewise through the data exchange
    rather than as a single timestamp, and its hour runs 1-24 rather than 0-23.
    Both quirks are normalised here so downstream code never has to remember
    them.
    """

    year: int
    month: Annotated[int, Field(ge=1, le=12)]
    day: Annotated[int, Field(ge=1, le=31)]
    hour: Annotated[int, Field(ge=0, le=23)]
    minute: Annotated[int, Field(ge=0, le=59)]
    day_of_week: Annotated[int, Field(ge=1, le=7)]
    """1 = Sunday, matching the EnergyPlus convention."""

    @property
    def as_datetime(self) -> datetime:
        """The clock position as a naive :class:`datetime`.

        Returns:
            A timezone-naive datetime. Simulation time has no timezone: the
            weather file already encodes local standard time for the site, and
            attaching one would imply a DST handling that EnergyPlus does not
            perform here.
        """
        return datetime(self.year, self.month, self.day, self.hour, self.minute)

    @property
    def hour_of_day(self) -> float:
        """Fractional hour of day, for indexing hourly tariff and carbon signals.

        Returns:
            Hour plus the minute fraction, in ``[0.0, 24.0)``.
        """
        return self.hour + self.minute / 60.0

    def isoformat(self) -> str:
        """Render the clock position as an ISO-8601 string.

        Returns:
            The timestamp in ``YYYY-MM-DDTHH:MM:SS`` form.
        """
        return self.as_datetime.isoformat()


class ZoneTelemetry(_Frozen):
    """One thermal zone's state at a single timestep.

    ``pmv``, ``ppd_pct`` and ``co2_ppm`` are optional because they depend on
    output variables that must be injected into the IDF by ``ecoloop prepare``.
    A ``None`` means "this run cannot see that quantity" — which is a
    materially different statement from a zero, and the comfort metrics treat it
    as such rather than silently scoring an unmeasured zone as comfortable.
    """

    zone: str
    """Zone name, upper-cased to match the EnergyPlus internal convention."""

    air_temperature_c: float
    relative_humidity_pct: Percent
    heating_setpoint_c: float
    cooling_setpoint_c: float
    occupancy_fraction: Fraction
    pmv: float | None = None
    ppd_pct: Percent | None = None
    co2_ppm: float | None = None

    @property
    def deadband_c(self) -> float:
        """Gap between the cooling and heating setpoints.

        Returns:
            Cooling minus heating, in kelvin. Negative or zero means the zone is
            heating and cooling simultaneously, which raises energy use.
        """
        return self.cooling_setpoint_c - self.heating_setpoint_c

    def is_occupied(self, threshold: float) -> bool:
        """Whether the zone counts as occupied.

        Args:
            threshold: Fractional occupancy above which the zone is occupied,
                from ``comfort.occupied_threshold_fraction``.

        Returns:
            ``True`` when fractional occupancy exceeds the threshold.
        """
        return self.occupancy_fraction > threshold


class SiteConditions(_Frozen):
    """Outdoor conditions at the site for a single timestep."""

    outdoor_air_temperature_c: float
    outdoor_relative_humidity_pct: Percent
    direct_normal_radiation_w_m2: float = 0.0
    diffuse_horizontal_radiation_w_m2: float = 0.0
    wind_speed_m_s: float = 0.0


class MeterReading(_Frozen):
    """A single EnergyPlus meter value for the reporting period just ended.

    The value is in **Joules**. EnergyPlus meters always are, and the single
    most expensive unit error available in this project is to treat one as kWh
    or W. Conversion happens once, in :mod:`ecoloop.analysis`, using
    ``analysis.joules_per_kwh``.
    """

    name: str
    joules: float


class TelemetrySample(_Frozen):
    """A complete observation of the building at one simulation timestep.

    This is the only object the EnergyPlus callback publishes and the only
    building state the cognitive tier ever sees. It is a value, not a view: it
    holds no reference to EnergyPlus state, so the worker thread can hold onto
    it indefinitely without risking a call into a non-thread-safe API.
    """

    clock: SimClock
    timestep_index: int
    """Monotonic count of actuation-eligible timesteps within this environment."""

    environment: str
    """EnergyPlus environment name, e.g. the run period or a sizing period."""

    warmup: bool
    """True during warmup convergence. Such samples are physically meaningless
    and never enter metrics or LLM context; the field exists so a test can prove
    they were filtered rather than merely assumed absent."""

    site: SiteConditions
    zones: tuple[ZoneTelemetry, ...]
    meters: tuple[MeterReading, ...] = ()

    def zone(self, name: str) -> ZoneTelemetry | None:
        """Look up a zone by name, case-insensitively.

        EnergyPlus upper-cases most identifiers, so a lookup using the name as
        it appears in the IDF would otherwise miss.

        Args:
            name: Zone name in any casing.

        Returns:
            The matching zone, or ``None`` if this sample has no such zone.
        """
        wanted = name.strip().upper()
        return next((z for z in self.zones if z.zone == wanted), None)

    def meter(self, name: str) -> float:
        """Read one meter's Joules for this timestep.

        Args:
            name: Meter name as declared in the IDF, any casing.

        Returns:
            Joules accumulated over the timestep, or ``0.0`` if the meter is not
            part of this run.
        """
        wanted = name.strip().upper()
        return next((m.joules for m in self.meters if m.name.upper() == wanted), 0.0)

    @property
    def total_site_joules(self) -> float:
        """Sum of the facility-level meters.

        Returns:
            Facility electricity plus facility natural gas, in Joules. Heating
            in this building is ``Coil:Heating:Fuel``, so an electricity-only
            total understates site energy by the entire heating season.
            Electricity is read from ``ElectricityNet:Facility`` rather than
            ``Electricity:Facility`` — see the note in ``config/zones.yaml``
            (AGENTS.md landmine): the latter inexplicably fails to resolve
            through the EnergyPlus Python API despite being a perfectly real
            meter, while the two are numerically identical for a building with
            no on-site generation.
        """
        return self.meter("ElectricityNet:Facility") + self.meter("NaturalGas:Facility")
