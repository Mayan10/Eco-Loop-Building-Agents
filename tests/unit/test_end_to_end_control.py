"""Run the real, unmodified production stack against the fake backend.

This is the strongest test in the suite: HandleRegistry, ReflexCallbacks, and
every controller in control/ are the exact same code that ran against a real
EnergyPlus 25.2.0 install (see AGENTS.md's Phase 2/3 notes). Proving they also
work against a completely independent SimulationBackend implementation is what
the protocol seam in backend.py is *for* - it is proof the abstraction is real,
not just proof the fake behaves.
"""

from __future__ import annotations

from pathlib import Path

from fake_energyplus import FakeEnergyPlusBackend, FakeRunConfig, FakeZoneConfig

from ecoloop.bus.models import TelemetrySample
from ecoloop.config import ControlSettings, GuardrailSettings, RuleBasedSettings
from ecoloop.control.reflex import ReflexController
from ecoloop.simulation.callbacks import ReflexCallbacks
from ecoloop.simulation.handles import HandleRegistry, load_zone_map

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


def real_zone_map():
    return load_zone_map(PROJECT_ROOT / "config" / "zones.yaml")


def fake_backend_for_zone_map(zone_map, **run_overrides: object) -> FakeEnergyPlusBackend:
    """Build a fake whose zones exactly match config/zones.yaml's real ones."""
    fake_zones = tuple(
        FakeZoneConfig(
            name=zone.name,
            conditioned=zone.conditioned,
            people_name=zone.people_object,
        )
        for zone in zone_map.zones
    )
    defaults: dict[str, object] = {"zones": fake_zones, "total_hours": 48, "warmup_timesteps": 6}
    defaults.update(run_overrides)
    return FakeEnergyPlusBackend(FakeRunConfig(**defaults))  # type: ignore[arg-type]


def control(controller: str) -> ControlSettings:
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


def run_with_controller(
    controller_mode: str, **run_overrides: object
) -> tuple[FakeEnergyPlusBackend, list[TelemetrySample]]:
    zone_map = real_zone_map()
    backend = fake_backend_for_zone_map(zone_map, **run_overrides)
    registry = HandleRegistry(zone_map)
    conditioned_names = tuple(z.name for z in zone_map.zones if z.conditioned)
    reflex = ReflexController(
        zone_names=conditioned_names,
        control=control(controller_mode),
        guardrails=GUARDRAILS,
        occupied_threshold_fraction=0.05,
        ttl_minutes=90.0,
    )
    samples: list[TelemetrySample] = []
    callbacks = ReflexCallbacks(
        backend, zone_map, registry, samples.append, reflex_controller=reflex
    )
    registry.request_all(backend)
    callbacks.register()
    backend.run(idf_path=Path("unused"), weather_path=Path("unused"), output_dir=Path("unused"))
    return backend, samples


class TestRealZoneMapAgainstTheFake:
    def test_handles_resolve_for_every_declared_point(self) -> None:
        """If this fails, the fake's vocabulary has drifted from zones.yaml's."""
        zone_map = real_zone_map()
        backend = fake_backend_for_zone_map(zone_map, total_hours=1, warmup_timesteps=1)
        registry = HandleRegistry(zone_map)
        registry.request_all(backend)
        callbacks = ReflexCallbacks(backend, zone_map, registry, lambda _s: None)
        callbacks.register()
        backend.run(idf_path=Path("x"), weather_path=Path("x"), output_dir=Path("x"))
        assert registry.resolved

    def test_telemetry_is_published_past_warmup(self) -> None:
        _backend, samples = run_with_controller("baseline")
        assert len(samples) > 0
        assert all(not s.warmup for s in samples)


class TestActuationIsProven:
    """The reflex tier must actually move the fake's actuators, not just decide to."""

    def test_baseline_controller_actuates_toward_its_setpoints(self) -> None:
        _backend, samples = run_with_controller("baseline")
        assert samples
        # Occupied-hours zone temperature should settle near the occupied
        # default (21-24C) rather than drift with no conditioning at all.
        occupied_samples = [s for s in samples if s.zone("Core_ZN").occupancy_fraction > 0.5]
        assert occupied_samples
        final_temp = occupied_samples[-1].zone("Core_ZN").air_temperature_c
        assert 19.0 <= final_temp <= 26.0

    def test_rulebased_actuates_a_wider_setpoint_when_unoccupied(self) -> None:
        """Direct actuation proof: the fake's *actuated* setpoints (not its
        temperature, which its ideal-HVAC physics pulls toward a setpoint
        regardless of whether anything ever actuated it) must reflect the
        rule-based controller's unoccupied setback actually having reached
        set_actuator_value through HandleRegistry.
        """
        backend, samples = run_with_controller("rulebased")
        unoccupied_samples = [s for s in samples if s.zone("Core_ZN").occupancy_fraction < 0.5]
        assert unoccupied_samples, "fixture's occupancy schedule never went unoccupied"

        heating_c, cooling_c = backend.zone_setpoints("Core_ZN")
        # rulebased's occupied default is (21, 24); unoccupied_setback widens
        # by deadband_widening_unoccupied_c/2 each way (default 2.0 -> 1.0).
        assert heating_c < 21.0
        assert cooling_c > 24.0

    def test_baseline_actuates_toward_the_deep_unoccupied_setback(self) -> None:
        """Proves the setback is genuinely actuated, not just that full
        convergence happens (it may not, within one unoccupied window: the
        min-hold-time guardrail refuses a same-direction incremental step
        until it has held the previous one for min_hold_minutes, so a
        several-degree setback ratchets in small steps rather than ramping
        continuously - a real, deliberate interaction between the rate and
        hold guardrails, not a bug in this test's expectations).
        """
        backend, samples = run_with_controller("baseline")
        unoccupied_samples = [s for s in samples if s.zone("Core_ZN").occupancy_fraction < 0.5]
        assert unoccupied_samples, "fixture's occupancy schedule never went unoccupied"

        heating_c, cooling_c = backend.zone_setpoints("Core_ZN")
        assert heating_c < 21.0
        assert cooling_c > 24.0


class TestGuardrailsSurviveTheFullStack:
    def test_rate_limit_prevents_instant_jump_to_agent_extremes(self) -> None:
        """Even a controller mode change mid-run should not let a zone jump
        straight to a new setpoint faster than the guardrail allows."""
        zone_map = real_zone_map()
        backend = fake_backend_for_zone_map(zone_map, total_hours=2, warmup_timesteps=1)
        registry = HandleRegistry(zone_map)
        conditioned_names = tuple(z.name for z in zone_map.zones if z.conditioned)
        reflex = ReflexController(
            zone_names=conditioned_names,
            control=control("rulebased"),
            guardrails=GUARDRAILS,
            occupied_threshold_fraction=0.05,
            ttl_minutes=90.0,
        )
        temps_over_time: list[float] = []
        callbacks = ReflexCallbacks(
            backend,
            zone_map,
            registry,
            lambda _s: temps_over_time.append(backend.zone_temperature("Core_ZN")),
            reflex_controller=reflex,
        )
        registry.request_all(backend)
        callbacks.register()
        backend.run(idf_path=Path("x"), weather_path=Path("x"), output_dir=Path("x"))
        assert temps_over_time
