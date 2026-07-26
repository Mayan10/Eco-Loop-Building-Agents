"""Tests for the telemetry ring buffer."""

from __future__ import annotations

from ecoloop.bus.models import (
    MeterReading,
    SimClock,
    SiteConditions,
    TelemetrySample,
    ZoneTelemetry,
)
from ecoloop.bus.telemetry import TelemetryBus


def make_sample(minute: int) -> TelemetrySample:
    return TelemetrySample(
        clock=SimClock(year=1999, month=1, day=1, hour=0, minute=minute, day_of_week=1),
        timestep_index=minute,
        environment="environment-1",
        warmup=False,
        site=SiteConditions(outdoor_air_temperature_c=5.0, outdoor_relative_humidity_pct=50.0),
        zones=(
            ZoneTelemetry(
                zone="CORE_ZN",
                air_temperature_c=22.0,
                relative_humidity_pct=45.0,
                heating_setpoint_c=21.0,
                cooling_setpoint_c=24.0,
                occupancy_fraction=0.5,
            ),
        ),
        meters=(MeterReading(name="ElectricityNet:Facility", joules=1000.0),),
    )


class TestPublishing:
    def test_latest_returns_most_recent(self) -> None:
        bus = TelemetryBus(capacity=10)
        bus.put_nowait(make_sample(0))
        bus.put_nowait(make_sample(1))
        assert bus.latest() is not None
        assert bus.latest().clock.minute == 1  # type: ignore[union-attr]

    def test_latest_is_none_when_empty(self) -> None:
        assert TelemetryBus(capacity=10).latest() is None

    def test_len_tracks_buffer_size(self) -> None:
        bus = TelemetryBus(capacity=10)
        for i in range(3):
            bus.put_nowait(make_sample(i))
        assert len(bus) == 3

    def test_snapshot_is_oldest_first_and_independent(self) -> None:
        bus = TelemetryBus(capacity=10)
        bus.put_nowait(make_sample(0))
        bus.put_nowait(make_sample(1))
        snapshot = bus.snapshot()
        bus.put_nowait(make_sample(2))
        assert [s.clock.minute for s in snapshot] == [0, 1]


class TestDropOldest:
    def test_full_buffer_drops_oldest_without_blocking_or_raising(self) -> None:
        bus = TelemetryBus(capacity=2)
        bus.put_nowait(make_sample(0))
        bus.put_nowait(make_sample(1))
        bus.put_nowait(make_sample(2))
        assert [s.clock.minute for s in bus.snapshot()] == [1, 2]

    def test_drops_are_counted(self) -> None:
        bus = TelemetryBus(capacity=1)
        bus.put_nowait(make_sample(0))
        assert bus.dropped_count == 0
        bus.put_nowait(make_sample(1))
        assert bus.dropped_count == 1
        bus.put_nowait(make_sample(2))
        assert bus.dropped_count == 2

    def test_published_count_includes_dropped_samples(self) -> None:
        bus = TelemetryBus(capacity=1)
        bus.put_nowait(make_sample(0))
        bus.put_nowait(make_sample(1))
        assert bus.published_count == 2


class TestWindow:
    def test_window_returns_samples_within_trailing_minutes(self) -> None:
        bus = TelemetryBus(capacity=10)
        for minute in (0, 10, 20, 30):
            bus.put_nowait(make_sample(minute))
        windowed = bus.window(minutes=15)
        assert [s.clock.minute for s in windowed] == [20, 30]

    def test_window_on_empty_bus_is_empty(self) -> None:
        assert TelemetryBus(capacity=10).window(minutes=60) == ()

    def test_window_covering_everything_returns_all(self) -> None:
        bus = TelemetryBus(capacity=10)
        for minute in (0, 10, 20):
            bus.put_nowait(make_sample(minute))
        assert len(bus.window(minutes=999)) == 3
