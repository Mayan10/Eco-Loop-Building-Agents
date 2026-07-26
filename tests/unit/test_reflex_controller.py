"""Tests for ReflexController: mode selection, degradation, and clamping."""

from __future__ import annotations

from _sample_factory import make_sample, make_zone

from ecoloop.bus.models import SimClock
from ecoloop.bus.policy import ControlPolicy, PolicySource, PolicyStore, ZoneSetpoint
from ecoloop.config import ControlSettings, GuardrailSettings, RuleBasedSettings
from ecoloop.control.reflex import ReflexController

GUARDRAILS = GuardrailSettings(
    heating_setpoint_min_c=15.0,
    heating_setpoint_max_c=23.0,
    cooling_setpoint_min_c=21.0,
    cooling_setpoint_max_c=30.0,
    min_deadband_c=2.0,
    max_setpoint_change_per_hour_c=1.5,
    min_hold_minutes=30.0,
    zone_temp_alarm_min_c=12.0,
    zone_temp_alarm_max_c=32.0,
    min_lighting_fraction_occupied=0.6,
)


def control(controller: str = "agent") -> ControlSettings:
    return ControlSettings(
        controller=controller,  # type: ignore[arg-type]
        default_heating_setpoint_c=21.0,
        default_cooling_setpoint_c=24.0,
        unoccupied_heating_setpoint_c=15.6,
        unoccupied_cooling_setpoint_c=29.4,
        demand_cap_kw=45.0,
        demand_window_minutes=15.0,
        demand_trigger_fraction=0.9,
        rulebased=RuleBasedSettings(),
    )


class TestModeSelection:
    def test_baseline_mode_computes_baseline_policy_directly(self) -> None:
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("baseline"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
        )
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.0),))
        results = reflex.decide(sample)
        assert results["CORE_ZN"].heating_setpoint_c == 15.6

    def test_rulebased_mode_computes_rulebased_policy_directly(self) -> None:
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("rulebased"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
        )
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=10.0
        )
        results = reflex.decide(sample)
        assert results["CORE_ZN"].cooling_setpoint_c == 23.0  # economiser shift applied


class TestAgentDegradationLadder:
    def test_agent_mode_uses_fresh_policy_store_policy(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("agent"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
            policy_store=store,
        )
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),))
        store.publish(
            ControlPolicy(
                issued_at=sample.clock,
                source=PolicySource.AGENT,
                ttl_minutes=90.0,
                zone_setpoints=(
                    ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=19.0, cooling_setpoint_c=26.0),
                ),
            )
        )
        results = reflex.decide(sample)
        assert results["CORE_ZN"].heating_setpoint_c == 19.0
        assert results["CORE_ZN"].cooling_setpoint_c == 26.0

    def test_agent_mode_degrades_to_rulebased_when_store_is_empty(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("agent"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
            policy_store=store,
        )
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=10.0
        )
        results = reflex.decide(sample)
        assert results["CORE_ZN"].cooling_setpoint_c == 23.0  # rule-based economiser shift

    def test_agent_mode_degrades_to_rulebased_when_policy_expired(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        stale_clock = SimClock(year=1999, month=6, day=15, hour=0, minute=0, day_of_week=3)
        store.publish(
            ControlPolicy(
                issued_at=stale_clock,
                source=PolicySource.AGENT,
                ttl_minutes=5.0,  # expires long before the sample's clock
                zone_setpoints=(
                    ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=19.0, cooling_setpoint_c=26.0),
                ),
            )
        )
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("agent"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
            policy_store=store,
        )
        sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), outdoor_air_temperature_c=25.0
        )
        results = reflex.decide(sample)
        assert results["CORE_ZN"].heating_setpoint_c == 21.0  # rule-based default, not 19.0

    def test_agent_mode_without_a_policy_store_degrades_to_rulebased(self) -> None:
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("agent"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
            policy_store=None,
        )
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),))
        results = reflex.decide(sample)
        assert results["CORE_ZN"].heating_setpoint_c == 21.0


class TestGuardrailIntegration:
    def test_agent_proposal_outside_envelope_is_clamped(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("agent"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
            policy_store=store,
        )
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),))
        store.publish(
            ControlPolicy(
                issued_at=sample.clock,
                source=PolicySource.AGENT,
                ttl_minutes=90.0,
                zone_setpoints=(
                    ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=99.0, cooling_setpoint_c=99.0),
                ),
            )
        )
        results = reflex.decide(sample)
        assert results["CORE_ZN"].heating_setpoint_c == GUARDRAILS.heating_setpoint_max_c

    def test_zone_with_no_proposal_is_absent_from_results(self) -> None:
        reflex = ReflexController(
            zone_names=("CORE_ZN", "PERIMETER_ZN_1"),
            control=control("baseline"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
        )
        sample = make_sample(zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),))
        results = reflex.decide(sample)
        assert "PERIMETER_ZN_1" not in results

    def test_memory_persists_across_calls_and_enforces_rate_limit(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        reflex = ReflexController(
            zone_names=("CORE_ZN",),
            control=control("agent"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
            policy_store=store,
        )
        first_sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), minute=0, timestep_index=1
        )
        store.publish(
            ControlPolicy(
                issued_at=first_sample.clock,
                source=PolicySource.AGENT,
                ttl_minutes=90.0,
                zone_setpoints=(
                    ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=21.0, cooling_setpoint_c=24.0),
                ),
            )
        )
        reflex.decide(first_sample)

        # 40 minutes later: past min_hold_minutes (30), so this exercises the
        # rate cap rather than the hold-time refusal.
        second_sample = make_sample(
            zones=(make_zone("CORE_ZN", occupancy_fraction=0.8),), minute=40, timestep_index=2
        )
        store.publish(
            ControlPolicy(
                issued_at=second_sample.clock,
                source=PolicySource.AGENT,
                ttl_minutes=90.0,
                zone_setpoints=(
                    ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=23.0, cooling_setpoint_c=24.0),
                ),
            )
        )
        results = reflex.decide(second_sample)
        # 40 minutes elapsed, cap is 1.5C/hour -> max 1.0C move from 21.0
        assert results["CORE_ZN"].heating_setpoint_c == 22.0
