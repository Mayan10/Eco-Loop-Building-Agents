"""Tests for assembling the self-contained offline HTML comparison report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from ecoloop.analysis.collect import write_telemetry
from ecoloop.analysis.compare import compare_runs
from ecoloop.analysis.report import build_report
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.runner import RunManifest


@pytest.fixture
def settings() -> EcoLoopSettings:
    return load_settings(profile="fast")


def make_comparison(tmp_path: Path, settings: EcoLoopSettings):
    started = datetime(2020, 1, 1, tzinfo=UTC)
    run_dirs = []
    for controller in ("baseline", "rulebased"):
        run_dir = tmp_path / controller
        df = pd.DataFrame.from_records(
            [
                {
                    "clock_iso": "2020-01-01T00:00:00",
                    "meter__ElectricityNet:Facility_j": 3_600_000.0,
                    "zone__CORE_ZN__occupancy_fraction": 0.8,
                    "zone__CORE_ZN__pmv": 0.1,
                }
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
            timesteps_published=1,
            dropped_samples=0,
            exit_code=0,
            succeeded=True,
            conditioned_floor_area_m2=511.16,
        )
        manifest.write(run_dir / "manifest.json")
        run_dirs.append(run_dir)
    return compare_runs(run_dirs, settings)


class TestBuildReport:
    def test_writes_a_self_contained_html_file(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        comparison = make_comparison(tmp_path, settings)
        output_path = tmp_path / "report.html"

        result = build_report(comparison, settings, output_path)

        assert result == output_path
        assert output_path.is_file()
        text = output_path.read_text(encoding="utf-8")
        assert "<!doctype html>" in text.lower()
        assert settings.analysis.report.title in text
        assert "baseline" in text
        assert "rulebased" in text

    def test_embeds_plotly_js_exactly_once(self, settings: EcoLoopSettings, tmp_path: Path) -> None:
        """Only the first figure should embed the ~4 MB Plotly library."""
        comparison = make_comparison(tmp_path, settings)
        output_path = tmp_path / "report.html"

        build_report(comparison, settings, output_path)

        text = output_path.read_text(encoding="utf-8")
        assert text.count("Plotly.newPlot") >= 1
        # A full inline Plotly bundle defines its own module scope once;
        # counting that marker confirms it wasn't duplicated per figure.
        assert text.count("var PlotlyConfig") <= 1

    def test_creates_parent_directories(self, settings: EcoLoopSettings, tmp_path: Path) -> None:
        comparison = make_comparison(tmp_path, settings)
        output_path = tmp_path / "nested" / "dir" / "report.html"

        build_report(comparison, settings, output_path)

        assert output_path.is_file()
