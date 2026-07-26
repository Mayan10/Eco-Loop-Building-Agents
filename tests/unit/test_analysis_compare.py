"""Tests for comparing multiple runs' manifests and metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from ecoloop.analysis.collect import write_telemetry
from ecoloop.analysis.compare import compare_runs, find_latest_runs
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.errors import UnfairComparisonError
from ecoloop.runner import RunManifest


@pytest.fixture
def settings() -> EcoLoopSettings:
    return load_settings(profile="fast")


def make_run(
    run_dir: Path,
    *,
    controller: str,
    profile: str = "fast",
    weather_path: Path = Path("weather.epw"),
    energyplus_version: str = "25.2.0",
    started_at: datetime,
    total_electricity_j: float = 3_600_000.0,
) -> RunManifest:
    telemetry_path = run_dir / "telemetry.parquet"
    df = pd.DataFrame.from_records(
        [
            {
                "clock_iso": "2020-01-01T00:00:00",
                "meter__ElectricityNet:Facility_j": total_electricity_j,
                "zone__CORE_ZN__occupancy_fraction": 0.8,
                "zone__CORE_ZN__pmv": 0.1,
            }
        ]
    )
    write_telemetry(df, telemetry_path)

    manifest = RunManifest(
        controller=controller,
        profile=profile,
        energyplus_version=energyplus_version,
        started_at=started_at,
        ended_at=started_at,
        idf_path=run_dir / "prepared.idf",
        weather_path=weather_path,
        output_dir=run_dir,
        telemetry_path=telemetry_path,
        timesteps_published=1,
        dropped_samples=0,
        exit_code=0,
        succeeded=True,
        conditioned_floor_area_m2=511.16,
    )
    manifest.write(run_dir / "manifest.json")
    return manifest


class TestCompareRuns:
    def test_compares_two_fair_runs(self, settings: EcoLoopSettings, tmp_path: Path) -> None:
        started = datetime(2020, 1, 1, tzinfo=UTC)
        baseline_dir = tmp_path / "baseline"
        rulebased_dir = tmp_path / "rulebased"
        make_run(baseline_dir, controller="baseline", started_at=started)
        make_run(rulebased_dir, controller="rulebased", started_at=started)

        result = compare_runs([baseline_dir, rulebased_dir], settings)

        assert result.profile == "fast"
        assert result.entry("baseline") is not None
        assert result.entry("rulebased") is not None
        assert result.entry("agent") is None

    def test_mismatched_weather_is_refused(self, settings: EcoLoopSettings, tmp_path: Path) -> None:
        started = datetime(2020, 1, 1, tzinfo=UTC)
        baseline_dir = tmp_path / "baseline"
        rulebased_dir = tmp_path / "rulebased"
        make_run(
            baseline_dir, controller="baseline", started_at=started, weather_path=Path("a.epw")
        )
        make_run(
            rulebased_dir, controller="rulebased", started_at=started, weather_path=Path("b.epw")
        )

        with pytest.raises(UnfairComparisonError, match="fingerprint"):
            compare_runs([baseline_dir, rulebased_dir], settings)

    def test_mismatched_engine_version_is_refused(
        self, settings: EcoLoopSettings, tmp_path: Path
    ) -> None:
        started = datetime(2020, 1, 1, tzinfo=UTC)
        baseline_dir = tmp_path / "baseline"
        rulebased_dir = tmp_path / "rulebased"
        make_run(
            baseline_dir, controller="baseline", started_at=started, energyplus_version="25.2.0"
        )
        make_run(
            rulebased_dir,
            controller="rulebased",
            started_at=started,
            energyplus_version="24.1.0",
        )

        with pytest.raises(UnfairComparisonError):
            compare_runs([baseline_dir, rulebased_dir], settings)

    def test_empty_run_dirs_raises_value_error(self, settings: EcoLoopSettings) -> None:
        with pytest.raises(ValueError, match="at least one"):
            compare_runs([], settings)


class TestFindLatestRuns:
    def test_picks_the_most_recently_started_run_per_controller(self, tmp_path: Path) -> None:
        older = datetime(2020, 1, 1, tzinfo=UTC)
        newer = datetime(2020, 6, 1, tzinfo=UTC)
        old_dir = tmp_path / "fast" / "baseline" / "old"
        new_dir = tmp_path / "fast" / "baseline" / "new"
        make_run(old_dir, controller="baseline", started_at=older)
        make_run(new_dir, controller="baseline", started_at=newer)

        latest = find_latest_runs(tmp_path)

        assert latest["baseline"] == new_dir

    def test_returns_one_entry_per_controller(self, tmp_path: Path) -> None:
        started = datetime(2020, 1, 1, tzinfo=UTC)
        make_run(tmp_path / "fast" / "baseline" / "r1", controller="baseline", started_at=started)
        make_run(tmp_path / "fast" / "rulebased" / "r1", controller="rulebased", started_at=started)

        latest = find_latest_runs(tmp_path)

        assert set(latest.keys()) == {"baseline", "rulebased"}

    def test_empty_root_yields_empty_mapping(self, tmp_path: Path) -> None:
        assert find_latest_runs(tmp_path) == {}
