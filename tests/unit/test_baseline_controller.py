"""Tests for BaselineController."""

from __future__ import annotations

from _sample_factory import make_sample, make_zone

from ecoloop.bus.policy import PolicySource
from ecoloop.config import ControlSettings
from ecoloop.control.baseline import BaselineController

CONTROL = ControlSettings(
    controller="baseline",
    default_heating_setpoint_c=21.0,
    default_cooling_setpoint_c=24.0,
    unoccupied_heating_setpoint_c=15.6,
    unoccupied_cooling_setpoint_c=29.4,
    demand_cap_kw=45.0,
    demand_window_minutes=15.0,
    demand_trigger_fraction=0.9,
)


def controller(zone_names: tuple[str, ...] = ("CORE_ZN",)) -> BaselineController:
    return BaselineController(
        zone_names=zone_names,
        control=CONTROL,
        occupied_threshold_fraction=0.05,
        ttl_minutes=90.0,
    )


class TestBaselineController:
    def test_occupied_zone_gets_default_setpoints(self) -> None:
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),))
        policy = controller().decide(sample)
        setpoint = policy.zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 21.0
        assert setpoint.cooling_setpoint_c == 24.0

    def test_unoccupied_zone_gets_setback_setpoints(self) -> None:
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.0),))
        policy = controller().decide(sample)
        setpoint = policy.zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 15.6
        assert setpoint.cooling_setpoint_c == 29.4

    def test_policy_is_tagged_as_baseline(self) -> None:
        sample = make_sample(zones=(make_zone("CORE_ZN"),))
        policy = controller().decide(sample)
        assert policy.source == PolicySource.BASELINE

    def test_zone_not_present_in_sample_is_skipped(self) -> None:
        sample = make_sample(zones=(make_zone("CORE_ZN"),))
        policy = controller(zone_names=("CORE_ZN", "PERIMETER_ZN_1")).decide(sample)
        assert policy.zone("PERIMETER_ZN_1") is None
        assert policy.zone("CORE_ZN") is not None

    def test_never_applies_economiser_or_setback_widening_logic(self) -> None:
        """Baseline is deliberately naive: outdoor conditions must not affect it."""
        cold_sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=-10.0
        )
        warm_sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=35.0
        )
        cold_policy = controller().decide(cold_sample)
        warm_policy = controller().decide(warm_sample)
        assert cold_policy.zone("CORE_ZN") == warm_policy.zone("CORE_ZN")
