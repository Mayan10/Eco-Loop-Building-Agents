"""Physics sanity tests for the fake EnergyPlus backend itself.

These are not about the control stack - they check that the fake's own
lumped-capacitance model behaves the way its docstring claims, so a test
built on top of it (see test_end_to_end_control.py) can trust its foundation.
"""

from __future__ import annotations

from fake_energyplus import FakeEnergyPlusBackend, FakeRunConfig, FakeZoneConfig

from ecoloop.simulation.backend import INVALID_HANDLE


def backend(**overrides: object) -> FakeEnergyPlusBackend:
    defaults: dict[str, object] = {
        "zones": (
            FakeZoneConfig(name="CORE_ZN", people_name="Core_ZN People", initial_temp_c=22.0),
        ),
        "total_hours": 4,
        "warmup_timesteps": 2,
    }
    defaults.update(overrides)
    return FakeEnergyPlusBackend(FakeRunConfig(**defaults))  # type: ignore[arg-type]


class TestClockAndReadiness:
    def test_not_ready_before_first_timestep(self) -> None:
        fake = backend()
        assert fake.api_data_fully_ready() is False

    def test_warmup_flag_clears_after_configured_timesteps(self) -> None:
        fake = backend()
        warmup_states = []
        fake.on_end_zone_timestep(lambda: warmup_states.append(fake.warmup_flag()))
        fake.run(idf_path=None, weather_path=None, output_dir=None)  # type: ignore[arg-type]
        assert warmup_states[:2] == [True, True]
        assert all(not w for w in warmup_states[2:])

    def test_is_weather_run_period_matches_not_warmup(self) -> None:
        fake = backend()
        states = []
        fake.on_end_zone_timestep(lambda: states.append(fake.is_weather_run_period()))
        fake.run(idf_path=None, weather_path=None, output_dir=None)  # type: ignore[arg-type]
        assert states[:2] == [False, False]
        assert all(states[2:])


class TestVariableResolution:
    def test_known_zone_variable_resolves(self) -> None:
        fake = backend()
        handle = fake.get_variable_handle("Zone Mean Air Temperature", "CORE_ZN")
        assert handle != INVALID_HANDLE

    def test_unknown_variable_is_invalid(self) -> None:
        fake = backend()
        assert fake.get_variable_handle("Not A Real Variable", "CORE_ZN") == INVALID_HANDLE

    def test_unknown_zone_key_is_invalid(self) -> None:
        fake = backend()
        assert fake.get_variable_handle("Zone Mean Air Temperature", "NOT_A_ZONE") == INVALID_HANDLE

    def test_pmv_is_keyed_on_people_name_not_zone_name(self) -> None:
        fake = backend()
        assert fake.get_variable_handle("Zone Thermal Comfort Fanger Model PMV", "CORE_ZN") == (
            INVALID_HANDLE
        )
        assert (
            fake.get_variable_handle("Zone Thermal Comfort Fanger Model PMV", "Core_ZN People")
            != INVALID_HANDLE
        )

    def test_meter_handles_resolve_for_declared_meters(self) -> None:
        fake = backend()
        assert fake.get_meter_handle("ElectricityNet:Facility") != INVALID_HANDLE
        assert fake.get_meter_handle("NotAMeter") == INVALID_HANDLE

    def test_actuator_resolves_for_conditioned_zone(self) -> None:
        fake = backend()
        handle = fake.get_actuator_handle("Zone Temperature Control", "Heating Setpoint", "CORE_ZN")
        assert handle != INVALID_HANDLE

    def test_actuator_does_not_resolve_for_unknown_zone(self) -> None:
        fake = backend()
        handle = fake.get_actuator_handle(
            "Zone Temperature Control", "Heating Setpoint", "NOT_A_ZONE"
        )
        assert handle == INVALID_HANDLE


class TestPhysics:
    def test_heating_raises_zone_temperature_toward_setpoint(self) -> None:
        fake = backend(total_hours=24, outdoor_temp_mean_c=5.0, outdoor_temp_amplitude_c=2.0)
        handle = fake.get_actuator_handle("Zone Temperature Control", "Heating Setpoint", "CORE_ZN")
        fake.set_actuator_value(handle, 25.0)
        fake.run(idf_path=None, weather_path=None, output_dir=None)  # type: ignore[arg-type]
        assert fake.zone_temperature("CORE_ZN") > 22.0

    def test_cooling_lowers_zone_temperature_toward_setpoint(self) -> None:
        fake = backend(total_hours=24, outdoor_temp_mean_c=35.0, outdoor_temp_amplitude_c=2.0)
        heating = fake.get_actuator_handle(
            "Zone Temperature Control", "Heating Setpoint", "CORE_ZN"
        )
        cooling = fake.get_actuator_handle(
            "Zone Temperature Control", "Cooling Setpoint", "CORE_ZN"
        )
        fake.set_actuator_value(heating, 15.0)
        fake.set_actuator_value(cooling, 18.0)
        fake.run(idf_path=None, weather_path=None, output_dir=None)  # type: ignore[arg-type]
        assert fake.zone_temperature("CORE_ZN") < 22.0

    def test_cooling_energy_accumulates_in_the_electricity_meter(self) -> None:
        fake = backend(total_hours=24, outdoor_temp_mean_c=35.0, outdoor_temp_amplitude_c=2.0)
        cooling = fake.get_actuator_handle(
            "Zone Temperature Control", "Cooling Setpoint", "CORE_ZN"
        )
        fake.set_actuator_value(cooling, 18.0)
        fake.run(idf_path=None, weather_path=None, output_dir=None)  # type: ignore[arg-type]
        assert fake.meter_total("ElectricityNet:Facility") > 0.0

    def test_ppd_tracks_pmv_via_the_real_ashrae_relationship(self) -> None:
        """PPD is not independently modelled - it must be a pure function of PMV."""
        fake = backend()
        fake.run(idf_path=None, weather_path=None, output_dir=None)  # type: ignore[arg-type]
        pmv_handle = fake.get_variable_handle(
            "Zone Thermal Comfort Fanger Model PMV", "Core_ZN People"
        )
        ppd_handle = fake.get_variable_handle(
            "Zone Thermal Comfort Fanger Model PPD", "Core_ZN People"
        )
        pmv = fake.get_variable_value(pmv_handle)
        ppd = fake.get_variable_value(ppd_handle)
        expected_ppd = 100.0 - 95.0 * pow(2.718281828, -(0.03353 * pmv**4 + 0.2179 * pmv**2))
        assert abs(ppd - expected_ppd) < 0.01
        assert ppd >= 5.0  # ASHRAE PPD is never modelled below 5%
