"""Tests for RuleBasedController."""

from __future__ import annotations

from _sample_factory import make_sample, make_zone

from ecoloop.bus.policy import PolicySource
from ecoloop.config import ControlSettings, RuleBasedSettings
from ecoloop.control.rulebased import RuleBasedController

RULEBASED = RuleBasedSettings(
    setback_enabled=True,
    deadband_widening_unoccupied_c=2.0,
    economiser_enabled=True,
    economiser_max_oat_c=18.0,
    economiser_min_oat_c=4.0,
    economiser_setpoint_shift_c=1.0,
)

CONTROL = ControlSettings(
    controller="rulebased",
    default_heating_setpoint_c=21.0,
    default_cooling_setpoint_c=24.0,
    unoccupied_heating_setpoint_c=15.6,
    unoccupied_cooling_setpoint_c=29.4,
    demand_cap_kw=45.0,
    demand_window_minutes=15.0,
    demand_trigger_fraction=0.9,
    rulebased=RULEBASED,
)


def controller(zone_names: tuple[str, ...] = ("CORE_ZN",)) -> RuleBasedController:
    return RuleBasedController(
        zone_names=zone_names,
        control=CONTROL,
        occupied_threshold_fraction=0.05,
        ttl_minutes=90.0,
    )


class TestRuleBasedController:
    def test_occupied_mild_weather_matches_baseline_default(self) -> None:
        """No measure fires: occupied, and outdoor air outside the economiser band."""
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=25.0
        )
        setpoint = controller().decide(sample).zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 21.0
        assert setpoint.cooling_setpoint_c == 24.0

    def test_unoccupied_zone_gets_setback_widening(self) -> None:
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.0),), outdoor_air_temperature_c=25.0
        )
        setpoint = controller().decide(sample).zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 20.0
        assert setpoint.cooling_setpoint_c == 25.0

    def test_occupied_zone_in_economiser_band_gets_lower_cooling(self) -> None:
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=10.0
        )
        setpoint = controller().decide(sample).zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 21.0
        assert setpoint.cooling_setpoint_c == 23.0

    def test_unoccupied_and_economiser_measures_compose(self) -> None:
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.0),), outdoor_air_temperature_c=10.0
        )
        setpoint = controller().decide(sample).zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 20.0  # setback only touches heating
        assert setpoint.cooling_setpoint_c == 24.0  # +1 (setback) - 1 (economiser) = default

    def test_policy_is_tagged_as_rulebased(self) -> None:
        sample = make_sample(zones=(make_zone("CORE_ZN"),))
        assert controller().decide(sample).source == PolicySource.RULEBASED

    def test_reasoning_names_the_measures_that_fired(self) -> None:
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.0),), outdoor_air_temperature_c=10.0
        )
        policy = controller().decide(sample)
        assert "unoccupied_setback" in policy.reasoning
        assert "economiser_shift" in policy.reasoning

    def test_reasoning_reports_nothing_fired_when_baseline_conditions(self) -> None:
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=25.0
        )
        policy = controller().decide(sample)
        assert "no measure applied" in policy.reasoning

    def test_disabling_a_measure_via_config_takes_effect(self) -> None:
        no_economiser = CONTROL.model_copy(
            update={"rulebased": RULEBASED.model_copy(update={"economiser_enabled": False})}
        )
        no_econ_controller = RuleBasedController(
            zone_names=("CORE_ZN",),
            control=no_economiser,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
        )
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=10.0
        )
        setpoint = no_econ_controller.decide(sample).zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.cooling_setpoint_c == 24.0
