"""Tests for zone-map loading and EnergyPlus handle resolution.

These exercise :mod:`ecoloop.simulation.handles` against
:class:`~tests.unit._fake_backend.FakeBackend` rather than a real engine, which
is the entire point of :class:`~ecoloop.simulation.backend.SimulationBackend`
being a protocol.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _fake_backend import FakeBackend

from ecoloop.errors import ConfigError, HandleResolutionError
from ecoloop.simulation.handles import HandleRegistry, ZoneMap, load_zone_map


def small_zone_map() -> ZoneMap:
    """A two-zone map: one conditioned zone with occupants, one bare plenum."""
    return ZoneMap.model_validate(
        {
            "version": 1,
            "model": "models/baseline/small_office.idf",
            "zones": [
                {
                    "name": "Core_ZN",
                    "role": "core",
                    "conditioned": True,
                    "people_object": "Core_ZN People",
                },
                {
                    "name": "Attic",
                    "role": "plenum",
                    "conditioned": False,
                    "people_object": None,
                },
            ],
            "zone_variables": {
                "air_temperature_c": {
                    "variable": "Zone Mean Air Temperature",
                    "key": "{zone}",
                    "required": True,
                },
                "relative_humidity_pct": {
                    "variable": "Zone Air Relative Humidity",
                    "key": "{zone}",
                    "required": True,
                },
                "heating_setpoint_c": {
                    "variable": "Zone Thermostat Heating Setpoint Temperature",
                    "key": "{zone}",
                    "required": False,
                },
                "cooling_setpoint_c": {
                    "variable": "Zone Thermostat Cooling Setpoint Temperature",
                    "key": "{zone}",
                    "required": False,
                },
                "occupancy_fraction": {
                    "variable": "Zone People Occupant Count",
                    "key": "{zone}",
                    "required": False,
                },
                "pmv": {
                    "variable": "Zone Thermal Comfort Fanger Model PMV",
                    "key": "{people}",
                    "required": False,
                },
                "ppd_pct": {
                    "variable": "Zone Thermal Comfort Fanger Model PPD",
                    "key": "{people}",
                    "required": False,
                },
                "co2_ppm": {
                    "variable": "Zone Air CO2 Concentration",
                    "key": "{zone}",
                    "required": False,
                },
            },
            "site_variables": {
                "outdoor_air_temperature_c": {
                    "variable": "Site Outdoor Air Drybulb Temperature",
                    "key": "Environment",
                    "required": True,
                },
                "outdoor_relative_humidity_pct": {
                    "variable": "Site Outdoor Air Relative Humidity",
                    "key": "Environment",
                    "required": True,
                },
            },
            "meters": ["ElectricityNet:Facility"],
            "zone_actuators": {
                "heating_setpoint_c": {
                    "component_type": "Zone Temperature Control",
                    "control_type": "Heating Setpoint",
                    "key": "{zone}",
                },
                "cooling_setpoint_c": {
                    "component_type": "Zone Temperature Control",
                    "control_type": "Cooling Setpoint",
                    "key": "{zone}",
                },
            },
            "global_actuators": {},
        }
    )


def fully_populated_backend() -> FakeBackend:
    """A backend with every point in ``small_zone_map`` resolvable."""
    backend = FakeBackend()
    backend.allow_variable("Site Outdoor Air Drybulb Temperature", "Environment", 5.0)
    backend.allow_variable("Site Outdoor Air Relative Humidity", "Environment", 55.0)
    for zone, temp in (("Core_ZN", 22.0), ("Attic", 15.0)):
        backend.allow_variable("Zone Mean Air Temperature", zone, temp)
        backend.allow_variable("Zone Air Relative Humidity", zone, 45.0)
        backend.allow_variable("Zone Air CO2 Concentration", zone, 500.0)
    backend.allow_variable("Zone Thermostat Heating Setpoint Temperature", "Core_ZN", 21.0)
    backend.allow_variable("Zone Thermostat Cooling Setpoint Temperature", "Core_ZN", 24.0)
    backend.allow_variable("Zone People Occupant Count", "Core_ZN", 0.8)
    backend.allow_variable("Zone Thermal Comfort Fanger Model PMV", "Core_ZN People", 0.1)
    backend.allow_variable("Zone Thermal Comfort Fanger Model PPD", "Core_ZN People", 5.2)
    backend.allow_meter("ElectricityNet:Facility", 3_600_000.0)
    backend.allow_actuator("Zone Temperature Control", "Heating Setpoint", "Core_ZN")
    backend.allow_actuator("Zone Temperature Control", "Cooling Setpoint", "Core_ZN")
    backend.ready = True
    return backend


class TestLoadZoneMap:
    def test_loads_the_real_shipped_zone_map(self, project_root: Path) -> None:
        zone_map = load_zone_map(project_root / "config" / "zones.yaml")
        assert zone_map.zone("core_zn") is not None
        assert zone_map.zone("CORE_ZN") is not None

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_zone_map(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.yaml"
        path.write_text("version: not-an-int\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="failed validation"):
            load_zone_map(path)


class TestRequestAll:
    def test_requests_site_and_zone_variables(self) -> None:
        backend = FakeBackend()
        HandleRegistry(small_zone_map()).request_all(backend)
        assert ("Site Outdoor Air Drybulb Temperature", "Environment") in backend.requested
        assert ("Zone Mean Air Temperature", "Core_ZN") in backend.requested
        assert ("Zone Mean Air Temperature", "Attic") in backend.requested

    def test_people_keyed_variable_skipped_for_zone_without_people(self) -> None:
        backend = FakeBackend()
        HandleRegistry(small_zone_map()).request_all(backend)
        assert ("Zone Thermal Comfort Fanger Model PMV", "Core_ZN People") in backend.requested
        assert not any(
            key == "Attic" and var.startswith("Zone Thermal") for var, key in backend.requested
        )


class TestEnsureResolved:
    def test_not_ready_is_a_no_op(self) -> None:
        backend = fully_populated_backend()
        backend.ready = False
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        assert not registry.resolved

    def test_resolves_once_ready(self) -> None:
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(fully_populated_backend())
        assert registry.resolved

    def test_idempotent_second_call_is_harmless(self) -> None:
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        registry.ensure_resolved(backend)  # must not raise or re-resolve
        assert registry.resolved

    def test_missing_required_site_variable_raises(self) -> None:
        backend = fully_populated_backend()
        backend._variable_handles.pop(("Site Outdoor Air Drybulb Temperature", "Environment"))
        registry = HandleRegistry(small_zone_map())
        with pytest.raises(HandleResolutionError, match="required variable"):
            registry.ensure_resolved(backend)

    def test_missing_required_zone_variable_raises(self) -> None:
        backend = fully_populated_backend()
        backend._variable_handles.pop(("Zone Mean Air Temperature", "Core_ZN"))
        registry = HandleRegistry(small_zone_map())
        with pytest.raises(HandleResolutionError, match="required variable"):
            registry.ensure_resolved(backend)

    def test_missing_optional_variable_does_not_raise(self) -> None:
        backend = fully_populated_backend()
        backend._variable_handles.pop(("Zone Thermal Comfort Fanger Model PMV", "Core_ZN People"))
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)  # must not raise
        assert registry.resolved

    def test_missing_meter_raises(self) -> None:
        backend = fully_populated_backend()
        backend._meter_handles.pop("ElectricityNet:Facility")
        registry = HandleRegistry(small_zone_map())
        with pytest.raises(HandleResolutionError, match="meter"):
            registry.ensure_resolved(backend)

    def test_missing_actuator_on_conditioned_zone_raises(self) -> None:
        backend = fully_populated_backend()
        backend._actuator_handles.pop(("Zone Temperature Control", "Heating Setpoint", "Core_ZN"))
        registry = HandleRegistry(small_zone_map())
        with pytest.raises(HandleResolutionError, match="actuator"):
            registry.ensure_resolved(backend)

    def test_unconditioned_zone_never_needs_an_actuator(self) -> None:
        """Attic has no thermostat in the IDF; resolution must not require one."""
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(fully_populated_backend())  # Attic actuators never registered
        assert registry.resolved

    def test_failed_resolution_is_not_retried_every_timestep(self) -> None:
        """A missing handle must raise exactly once, not on every later call.

        Without this, one missing meter or actuator would re-attempt full
        resolution (and re-raise) on every remaining timestep of a run —
        turning a single configuration error into thousands of expensive
        logged exceptions that look like a performance bug.
        """
        backend = fully_populated_backend()
        backend._meter_handles.pop("ElectricityNet:Facility")
        registry = HandleRegistry(small_zone_map())

        with pytest.raises(HandleResolutionError):
            registry.ensure_resolved(backend)

        # Make the meter resolvable now — if the registry retried, this second
        # call would succeed and resolved would flip to True. It must not.
        backend.allow_meter("ElectricityNet:Facility", 1.0)
        registry.ensure_resolved(backend)  # must be a silent no-op, not a retry
        assert not registry.resolved


class TestReading:
    def test_read_before_resolved_raises(self) -> None:
        registry = HandleRegistry(small_zone_map())
        with pytest.raises(HandleResolutionError, match="not resolved"):
            registry.read_zone_telemetry(fully_populated_backend(), "Core_ZN")

    def test_read_zone_telemetry_populates_optional_fields(self) -> None:
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        telemetry = registry.read_zone_telemetry(backend, "Core_ZN")
        assert telemetry.zone == "CORE_ZN"
        assert telemetry.air_temperature_c == pytest.approx(22.0)
        assert telemetry.pmv == pytest.approx(0.1)
        assert telemetry.ppd_pct == pytest.approx(5.2)
        assert telemetry.co2_ppm == pytest.approx(500.0)

    def test_read_zone_telemetry_leaves_unavailable_fields_none(self) -> None:
        """Attic has no thermostat and no people object; both must read as None."""
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        telemetry = registry.read_zone_telemetry(backend, "Attic")
        assert telemetry.pmv is None
        assert telemetry.ppd_pct is None
        assert telemetry.heating_setpoint_c == 0.0

    def test_read_site_conditions(self) -> None:
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        site = registry.read_site_conditions(backend)
        assert site.outdoor_air_temperature_c == pytest.approx(5.0)
        assert site.outdoor_relative_humidity_pct == pytest.approx(55.0)

    def test_read_meters_returns_joules(self) -> None:
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        meters = registry.read_meters(backend)
        assert meters[0].name == "ElectricityNet:Facility"
        assert meters[0].joules == pytest.approx(3_600_000.0)

    def test_actuator_handle_resolves(self) -> None:
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        handle = registry.actuator_handle("Core_ZN", "heating_setpoint_c")
        backend.set_actuator_value(handle, 20.5)
        assert backend._actuator_values[handle] == pytest.approx(20.5)

    def test_actuator_handle_missing_for_unconditioned_zone_raises(self) -> None:
        backend = fully_populated_backend()
        registry = HandleRegistry(small_zone_map())
        registry.ensure_resolved(backend)
        with pytest.raises(HandleResolutionError, match="no such actuator"):
            registry.actuator_handle("Attic", "heating_setpoint_c")
