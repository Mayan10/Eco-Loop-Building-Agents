"""Resolve the zone point map in ``config/zones.yaml`` against a running backend.

Two-phase lifecycle, matching the two EnergyPlus landmines this module exists
to avoid:

1. :meth:`HandleRegistry.request_all` — called **before** the run starts, so
   every variable is registered while EnergyPlus is still willing to accept
   requests (AGENTS.md landmine #2).
2. :meth:`HandleRegistry.ensure_resolved` — called from the first callback
   where the backend reports ``api_data_fully_ready()``; idempotent, so the
   callback can call it unconditionally on every timestep and it only does
   real work once (AGENTS.md landmine #3).

A handle that fails to resolve is not automatically an error: a point marked
``required: false`` in ``zones.yaml`` (PMV, PPD, CO2 — anything gated on an
IDF injection that ``prepare`` may or may not have performed) resolves to
``None`` and the corresponding :class:`~ecoloop.bus.models.ZoneTelemetry`
field is simply absent for the run. A point marked ``required: true`` failing
to resolve is impossible to run against — the building itself has no such
sensor — so it raises :class:`~ecoloop.errors.HandleResolutionError` naming
the exact variable, key, and zone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ecoloop.bus.models import MeterReading, SiteConditions, ZoneTelemetry
from ecoloop.errors import ConfigError, HandleResolutionError
from ecoloop.logging import get_logger
from ecoloop.simulation.backend import INVALID_HANDLE, SimulationBackend

__all__ = [
    "HandleRegistry",
    "ZoneDefinition",
    "ZoneMap",
    "load_zone_map",
]

_logger = get_logger(__name__, component="simulation")

_PEOPLE_KEY_TEMPLATE = "{people}"
_ZONE_KEY_TEMPLATE = "{zone}"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ZoneDefinition(_Frozen):
    """One zone entry from ``config/zones.yaml``."""

    name: str
    role: str
    conditioned: bool
    people_object: str | None = None
    orientation: str | None = None


class VariablePoint(_Frozen):
    """One ``Output:Variable`` request, with its key template."""

    variable: str
    key: str
    required: bool


class ActuatorPoint(_Frozen):
    """One actuator declaration."""

    component_type: str
    control_type: str
    key: str


NonEmptyStr = Annotated[str, Field(min_length=1)]


class ZoneMap(_Frozen):
    """The complete parsed contents of ``config/zones.yaml``."""

    version: int
    model: Path
    zones: tuple[ZoneDefinition, ...]
    zone_variables: dict[NonEmptyStr, VariablePoint]
    site_variables: dict[NonEmptyStr, VariablePoint]
    meters: tuple[str, ...]
    zone_actuators: dict[NonEmptyStr, ActuatorPoint]
    global_actuators: dict[NonEmptyStr, ActuatorPoint]

    def zone(self, name: str) -> ZoneDefinition | None:
        """Look up a zone definition by name, case-insensitively.

        Args:
            name: Zone name in any casing.

        Returns:
            The matching definition, or ``None``.
        """
        wanted = name.strip().upper()
        return next((z for z in self.zones if z.name.strip().upper() == wanted), None)


def load_zone_map(path: Path) -> ZoneMap:
    """Load and validate ``config/zones.yaml``.

    Args:
        path: Path to the zone map YAML file.

    Returns:
        The parsed, validated zone map.

    Raises:
        ConfigError: If the file is missing or fails schema validation.
    """
    if not path.is_file():
        raise ConfigError("zone map not found", path=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ZoneMap.model_validate(raw)
    except Exception as exc:
        raise ConfigError("zone map failed validation", path=str(path), cause=str(exc)) from exc


def _resolve_key(template: str, *, zone: ZoneDefinition) -> str | None:
    """Substitute a variable point's key template for one zone.

    Args:
        template: A key such as ``"{zone}"``, ``"{people}"``, or a literal.
        zone: The zone the point is being resolved for.

    Returns:
        The substituted key, or ``None`` if the template needs a People object
        this zone does not have (e.g. the unoccupied attic plenum).
    """
    if template == _PEOPLE_KEY_TEMPLATE:
        return zone.people_object
    if template == _ZONE_KEY_TEMPLATE:
        return zone.name
    return template


class HandleRegistry:
    """Resolved EnergyPlus handles for one run, keyed by the zone map's field names."""

    def __init__(self, zone_map: ZoneMap) -> None:
        """Create an empty registry bound to a zone map.

        Args:
            zone_map: The point map to resolve handles against.
        """
        self._zone_map = zone_map
        self._resolved = False
        self._resolution_failed = False
        self._site_variable_handles: dict[str, int | None] = {}
        self._zone_variable_handles: dict[tuple[str, str], int | None] = {}
        self._meter_handles: dict[str, int] = {}
        self._zone_actuator_handles: dict[tuple[str, str], int] = {}

    @property
    def resolved(self) -> bool:
        """Whether :meth:`ensure_resolved` has completed at least once."""
        return self._resolved

    def request_all(self, backend: SimulationBackend) -> None:
        """Request every declared variable, before the run starts.

        Args:
            backend: The backend to issue requests against.
        """
        for point in self._zone_map.site_variables.values():
            backend.request_variable(point.variable, point.key)

        for zone in self._zone_map.zones:
            for point in self._zone_map.zone_variables.values():
                key = _resolve_key(point.key, zone=zone)
                if key is not None:
                    backend.request_variable(point.variable, key)

    def ensure_resolved(self, backend: SimulationBackend) -> None:
        """Resolve every handle, exactly once, once the backend is ready.

        Safe to call on every timestep: it is a no-op before
        ``api_data_fully_ready()``, after the first successful resolution, and
        — just as importantly — after a *failed* resolution attempt. Without
        that last case, a single missing handle would raise on every
        remaining timestep of the run: each raise passes through the
        backend's callback guard (AGENTS.md invariant #1), which logs a full
        exception with traceback, and doing that thousands of times turns one
        configuration error into a multi-minute stall that looks like a
        performance bug rather than the missing-handle bug it actually is.
        The first failure still propagates — it must, to be seen at all —
        but every call after that is a silent no-op, same as if resolution
        had never been attempted.

        Args:
            backend: The backend to resolve handles against.
        """
        if self._resolved or self._resolution_failed or not backend.api_data_fully_ready():
            return

        try:
            self._resolve_site_variables(backend)
            self._resolve_zone_variables(backend)
            self._resolve_meters(backend)
            self._resolve_zone_actuators(backend)
        except HandleResolutionError:
            self._resolution_failed = True
            raise

        self._resolved = True
        _logger.info(
            "resolved EnergyPlus handles",
            zones=len(self._zone_map.zones),
            site_variables=len(self._site_variable_handles),
            zone_variables=len(self._zone_variable_handles),
            meters=len(self._meter_handles),
            actuators=len(self._zone_actuator_handles),
        )

    def _resolve_site_variables(self, backend: SimulationBackend) -> None:
        for field_name, point in self._zone_map.site_variables.items():
            handle = backend.get_variable_handle(point.variable, point.key)
            self._site_variable_handles[field_name] = self._checked(
                handle, point, key=point.key, zone=None
            )

    def _resolve_zone_variables(self, backend: SimulationBackend) -> None:
        for zone in self._zone_map.zones:
            for field_name, point in self._zone_map.zone_variables.items():
                key = _resolve_key(point.key, zone=zone)
                if key is None:
                    self._zone_variable_handles[(zone.name, field_name)] = None
                    continue
                handle = backend.get_variable_handle(point.variable, key)
                self._zone_variable_handles[(zone.name, field_name)] = self._checked(
                    handle, point, key=key, zone=zone.name
                )

    def _resolve_meters(self, backend: SimulationBackend) -> None:
        for meter in self._zone_map.meters:
            handle = backend.get_meter_handle(meter)
            if handle == INVALID_HANDLE:
                raise HandleResolutionError("meter does not exist in this run", meter=meter)
            self._meter_handles[meter] = handle

    def _resolve_zone_actuators(self, backend: SimulationBackend) -> None:
        for zone in self._zone_map.zones:
            if not zone.conditioned:
                continue
            for field_name, actuator in self._zone_map.zone_actuators.items():
                key = _resolve_key(actuator.key, zone=zone)
                assert key is not None  # noqa: S101 - actuator keys are always "{zone}"
                handle = backend.get_actuator_handle(
                    actuator.component_type, actuator.control_type, key
                )
                if handle == INVALID_HANDLE:
                    raise HandleResolutionError(
                        "actuator does not exist",
                        zone=zone.name,
                        component_type=actuator.component_type,
                        control_type=actuator.control_type,
                    )
                self._zone_actuator_handles[(zone.name, field_name)] = handle

    def _checked(
        self, handle: int, point: VariablePoint, *, key: str, zone: str | None
    ) -> int | None:
        """Apply a variable point's ``required`` policy to a resolved handle.

        Args:
            handle: The handle returned by the backend.
            point: The variable point being resolved.
            key: The key value used for this resolution attempt.
            zone: The zone name, if any, for error context.

        Returns:
            The handle if valid, or ``None`` if invalid and optional.

        Raises:
            HandleResolutionError: If invalid and the point is ``required``.
        """
        if handle != INVALID_HANDLE:
            return handle
        if point.required:
            raise HandleResolutionError(
                "required variable does not exist",
                variable=point.variable,
                key=key,
                zone=zone,
            )
        return None

    def read_zone_telemetry(self, backend: SimulationBackend, zone_name: str) -> ZoneTelemetry:
        """Read the current state of one zone.

        Args:
            backend: The backend to read values from.
            zone_name: Zone to read.

        Returns:
            A fully populated :class:`~ecoloop.bus.models.ZoneTelemetry`, with
            optional fields ``None`` wherever their handle did not resolve.

        Raises:
            HandleResolutionError: If handles have not been resolved yet, or a
            required field for this zone was never registered.
        """
        if not self._resolved:
            raise HandleResolutionError("handles not resolved yet", zone=zone_name)

        def read(field_name: str) -> float | None:
            handle = self._zone_variable_handles.get((zone_name, field_name))
            return None if handle is None else backend.get_variable_value(handle)

        air_temp = read("air_temperature_c")
        humidity = read("relative_humidity_pct")
        if air_temp is None or humidity is None:
            raise HandleResolutionError(
                "required zone variable missing at read time", zone=zone_name
            )

        return ZoneTelemetry(
            zone=zone_name.strip().upper(),
            air_temperature_c=air_temp,
            relative_humidity_pct=max(0.0, min(100.0, humidity)),
            heating_setpoint_c=read("heating_setpoint_c") or 0.0,
            cooling_setpoint_c=read("cooling_setpoint_c") or 0.0,
            occupancy_fraction=max(0.0, min(1.0, read("occupancy_fraction") or 0.0)),
            pmv=read("pmv"),
            ppd_pct=read("ppd_pct"),
            co2_ppm=read("co2_ppm"),
        )

    def read_site_conditions(self, backend: SimulationBackend) -> SiteConditions:
        """Read the current outdoor conditions.

        Args:
            backend: The backend to read values from.

        Returns:
            The site conditions for the current timestep.

        Raises:
            HandleResolutionError: If handles have not been resolved yet.
        """
        if not self._resolved:
            raise HandleResolutionError("handles not resolved yet")

        def read(field_name: str) -> float | None:
            handle = self._site_variable_handles.get(field_name)
            return None if handle is None else backend.get_variable_value(handle)

        outdoor_temp = read("outdoor_air_temperature_c")
        outdoor_rh = read("outdoor_relative_humidity_pct")
        if outdoor_temp is None or outdoor_rh is None:
            raise HandleResolutionError("required site variable missing at read time")

        return SiteConditions(
            outdoor_air_temperature_c=outdoor_temp,
            outdoor_relative_humidity_pct=max(0.0, min(100.0, outdoor_rh)),
            direct_normal_radiation_w_m2=read("direct_normal_radiation_w_m2") or 0.0,
            diffuse_horizontal_radiation_w_m2=read("diffuse_horizontal_radiation_w_m2") or 0.0,
            wind_speed_m_s=read("wind_speed_m_s") or 0.0,
        )

    def read_meters(self, backend: SimulationBackend) -> tuple[MeterReading, ...]:
        """Read every declared meter for the reporting period just ended.

        Args:
            backend: The backend to read values from.

        Returns:
            One :class:`~ecoloop.bus.models.MeterReading` per declared meter,
            in Joules.

        Raises:
            HandleResolutionError: If handles have not been resolved yet.
        """
        if not self._resolved:
            raise HandleResolutionError("handles not resolved yet")
        return tuple(
            MeterReading(name=name, joules=backend.get_meter_value(handle))
            for name, handle in self._meter_handles.items()
        )

    def actuator_handle(self, zone_name: str, field_name: str) -> int:
        """Look up a resolved zone actuator handle.

        Args:
            zone_name: Zone the actuator belongs to.
            field_name: Actuator field name from ``zones.yaml`` (e.g.
                ``"heating_setpoint_c"``).

        Returns:
            The resolved actuator handle.

        Raises:
            HandleResolutionError: If handles are unresolved, or this zone has
                no such actuator (e.g. the unconditioned attic).
        """
        handle = self._zone_actuator_handles.get((zone_name, field_name))
        if handle is None:
            raise HandleResolutionError(
                "no such actuator for zone", zone=zone_name, field=field_name
            )
        return handle
