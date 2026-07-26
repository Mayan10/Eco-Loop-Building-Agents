"""Tests for the Plotly figure builders behind the comparison report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pytest

from ecoloop.analysis.charts import (
    comfort_violation_chart,
    energy_breakdown_chart,
    energy_total_chart,
    pmv_timeseries_chart,
)
from ecoloop.analysis.collect import write_telemetry
from ecoloop.analysis.compare import compare_runs
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.runner import RunManifest


@pytest.fixture
def settings() -> EcoLoopSettings:
    return load_settings(profile="fast")


def make_comparison(tmp_path: Path, settings: EcoLoopSettings):
    started = datetime(2020, 1, 1, tzinfo=UTC)
    run_dirs = []
    for controller, electricity_j in (("baseline", 3_600_000.0), ("rulebased", 1_800_000.0)):
        run_dir = tmp_path / controller
        df = pd.DataFrame.from_records(
            [
                {
                    "clock_iso": "2020-01-01T00:00:00",
                    "meter__ElectricityNet:Facility_j": electricity_j,
                    "meter__Cooling:Electricity_j": electricity_j / 2,
                    "zone__CORE_ZN__occupancy_fraction": 0.8,
                    "zone__CORE_ZN__pmv": 0.9,
                },
                {
                    "clock_iso": "2020-01-01T00:15:00",
                    "meter__ElectricityNet:Facility_j": electricity_j,
                    "meter__Cooling:Electricity_j": electricity_j / 2,
                    "zone__CORE_ZN__occupancy_fraction": 0.8,
                    "zone__CORE_ZN__pmv": 0.1,
                },
            ]
        )
        write_telemetry(df, run_dir / "telemetry.parquet")
        manifest = RunManifest(
            controller=controller,
            profile="fast",
            energyplus_version="25.2.0",
            started_at=started,
            ended_at=started,
            idf_path=run_dir / "prepared.idf",
            weather_path=Path("weather.epw"),
            output_dir=run_dir,
            telemetry_path=run_dir / "telemetry.parquet",
            timesteps_published=2,
            dropped_samples=0,
            exit_code=0,
            succeeded=True,
            conditioned_floor_area_m2=511.16,
        )
        manifest.write(run_dir / "manifest.json")
        run_dirs.append(run_dir)
    return compare_runs(run_dirs, settings)


class TestEnergyTotalChart:
    def test_produces_one_bar_per_controller(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        comparison = make_comparison(tmp_path, settings)

        figure = energy_total_chart(comparison, settings)

        assert isinstance(figure, go.Figure)
        assert len(figure.data) == 1
        assert list(figure.data[0].x) == ["baseline", "rulebased"]


class TestEnergyBreakdownChart:
    def test_stacks_one_trace_per_meter(self, settings: EcoLoopSettings, tmp_path: Path) -> None:
        comparison = make_comparison(tmp_path, settings)

        figure = energy_breakdown_chart(comparison, settings)

        meter_names = {trace.name for trace in figure.data}
        assert meter_names == {"Cooling:Electricity", "ElectricityNet:Facility"}


class TestComfortViolationChart:
    def test_produces_one_bar_per_controller(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        comparison = make_comparison(tmp_path, settings)

        figure = comfort_violation_chart(comparison, settings)

        assert list(figure.data[0].x) == ["baseline", "rulebased"]


class TestPmvTimeseriesChart:
    def test_plots_a_line_per_controller_with_pmv(self, settings: EcoLoopSettings) -> None:
        telemetry_by_controller = {
            "baseline": pd.DataFrame.from_records(
                [{"clock_iso": "2020-01-01T00:00:00", "zone__CORE_ZN__pmv": 0.9}]
            ),
            "attic_only": pd.DataFrame.from_records(
                [{"clock_iso": "2020-01-01T00:00:00", "zone__ATTIC__pmv": None}]
            ),
        }

        figure = pmv_timeseries_chart(telemetry_by_controller, settings, zone="CORE_ZN")

        line_traces = [t for t in figure.data if t.type == "scatter"]
        assert len(line_traces) == 1
        assert line_traces[0].name == "baseline"
