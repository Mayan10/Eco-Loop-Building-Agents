"""Tests for flattening TelemetrySample streams into a tabular run history."""

from __future__ import annotations

from pathlib import Path

from ecoloop.analysis.collect import (
    TelemetryRecorder,
    flatten_sample,
    read_telemetry,
    write_telemetry,
)
from ecoloop.bus.models import (
    MeterReading,
    SimClock,
    SiteConditions,
    TelemetrySample,
    ZoneTelemetry,
)


def make_sample(*, minute: int = 0, cooling_joules: float = 1_000_000.0) -> TelemetrySample:
    return TelemetrySample(
        clock=SimClock(year=1999, month=7, day=15, hour=14, minute=minute, day_of_week=3),
        timestep_index=1,
        environment="environment-3",
        warmup=False,
        site=SiteConditions(outdoor_air_temperature_c=28.0, outdoor_relative_humidity_pct=50.0),
        zones=(
            ZoneTelemetry(
                zone="CORE_ZN",
                air_temperature_c=24.0,
                relative_humidity_pct=45.0,
                heating_setpoint_c=21.0,
                cooling_setpoint_c=24.0,
                occupancy_fraction=0.8,
                pmv=0.2,
                ppd_pct=8.0,
                co2_ppm=500.0,
            ),
            ZoneTelemetry(
                zone="ATTIC",
                air_temperature_c=30.0,
                relative_humidity_pct=40.0,
                heating_setpoint_c=15.6,
                cooling_setpoint_c=29.4,
                occupancy_fraction=0.0,
                pmv=None,
                ppd_pct=None,
                co2_ppm=None,
            ),
        ),
        meters=(MeterReading(name="Cooling:Electricity", joules=cooling_joules),),
    )


class TestFlattenSample:
    def test_flattens_clock_site_meter_and_zone_fields(self) -> None:
        row = flatten_sample(make_sample())

        assert row["clock_iso"] == "1999-07-15T14:00:00"
        assert row["site__outdoor_air_temperature_c"] == 28.0
        assert row["meter__Cooling:Electricity_j"] == 1_000_000.0
        assert row["zone__CORE_ZN__pmv"] == 0.2
        assert row["zone__CORE_ZN__occupancy_fraction"] == 0.8

    def test_none_comfort_fields_stay_none_not_zero(self) -> None:
        """An unconditioned zone with no PMV output must not silently read as 0.0."""
        row = flatten_sample(make_sample())

        assert row["zone__ATTIC__pmv"] is None
        assert row["zone__ATTIC__ppd_pct"] is None


class TestTelemetryRecorder:
    def test_records_every_sample_in_order(self) -> None:
        recorder = TelemetryRecorder()
        recorder.record(make_sample(minute=0))
        recorder.record(make_sample(minute=15))

        assert len(recorder) == 2
        df = recorder.to_dataframe()
        assert list(df["clock_iso"]) == ["1999-07-15T14:00:00", "1999-07-15T14:15:00"]

    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        recorder = TelemetryRecorder()
        recorder.record(make_sample())
        destination = tmp_path / "telemetry.parquet"

        recorder.write(destination)

        assert destination.is_file()
        df = read_telemetry(destination)
        assert len(df) == 1
        assert df.iloc[0]["meter__Cooling:Electricity_j"] == 1_000_000.0


class TestWriteTelemetryFunction:
    def test_writes_a_dataframe_directly(self, tmp_path: Path) -> None:
        recorder = TelemetryRecorder()
        recorder.record(make_sample())
        destination = tmp_path / "nested" / "telemetry.parquet"

        write_telemetry(recorder.to_dataframe(), destination)

        assert destination.is_file()
        assert len(read_telemetry(destination)) == 1
