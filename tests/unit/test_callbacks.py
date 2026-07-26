"""Tests for ReflexCallbacks against a FakeBackend.

No real EnergyPlus run happens here: FakeBackend lets tests fire the
begin-environment and end-timestep hooks directly and observe what gets
published, which is the whole point of registration and physics being
separate concerns.
"""

from __future__ import annotations

import pytest
from _fake_backend import FakeBackend
from test_handles import fully_populated_backend, small_zone_map

from ecoloop.bus.models import TelemetrySample
from ecoloop.simulation.callbacks import ReflexCallbacks
from ecoloop.simulation.handles import HandleRegistry


def build(backend: FakeBackend) -> tuple[ReflexCallbacks, list[TelemetrySample]]:
    samples: list[TelemetrySample] = []
    registry = HandleRegistry(small_zone_map())
    registry.request_all(backend)
    callbacks = ReflexCallbacks(backend, small_zone_map(), registry, samples.append)
    callbacks.register()
    return callbacks, samples


class TestWarmupAndReadinessGating:
    def test_no_sample_while_not_ready(self) -> None:
        backend = FakeBackend()
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        assert samples == []

    def test_no_sample_during_warmup(self) -> None:
        backend = fully_populated_backend()
        backend.warmup = True
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        assert samples == []

    def test_sample_published_once_ready_and_past_warmup(self) -> None:
        backend = fully_populated_backend()
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        assert len(samples) == 1
        assert samples[0].warmup is False

    def test_no_sample_during_sizing_periods(self) -> None:
        """Design-day sizing timesteps must never reach analysis or LLM context.

        EnergyPlus runs full physics for one or more sizing design days before
        the weather-file run period even begins, firing every callback
        registered here. Publishing those as telemetry would mix a design
        day's numbers into the run anyone actually asked for.
        """
        backend = fully_populated_backend()
        backend.weather_run_period = False
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        assert samples == []


class TestSampleContents:
    def test_clock_and_environment_are_carried_through(self) -> None:
        backend = fully_populated_backend()
        backend.clock_value = (1999, 6, 15, 14, 30, 3)
        backend.environment_name = "environment-2"
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        sample = samples[0]
        assert sample.clock.year == 1999
        assert sample.clock.month == 6
        assert sample.clock.hour == 14
        assert sample.environment == "environment-2"

    def test_zones_and_meters_are_populated(self) -> None:
        backend = fully_populated_backend()
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        sample = samples[0]
        assert sample.zone("Core_ZN") is not None
        assert sample.zone("Attic") is not None
        assert sample.total_site_joules == pytest.approx(3_600_000.0)


class TestTimestepIndexing:
    def test_index_increments_across_timesteps(self) -> None:
        backend = fully_populated_backend()
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        backend.fire_end_zone_timestep()
        assert [s.timestep_index for s in samples] == [1, 2]

    def test_index_resets_on_new_environment(self) -> None:
        backend = fully_populated_backend()
        _, samples = build(backend)
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        backend.fire_begin_new_environment()
        backend.fire_end_zone_timestep()
        assert [s.timestep_index for s in samples] == [1, 1]
