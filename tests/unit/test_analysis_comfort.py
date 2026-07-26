"""Tests for ASHRAE 55 comfort scoring from a run's telemetry history."""

from __future__ import annotations

import pandas as pd
import pytest

from ecoloop.analysis.comfort import compute_comfort_metrics
from ecoloop.config import EcoLoopSettings, load_settings


@pytest.fixture
def settings() -> EcoLoopSettings:
    return load_settings(profile="fast")


def make_df(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame.from_records(rows)


class TestComputeComfortMetrics:
    def test_unoccupied_zone_excluded_regardless_of_pmv(self, settings: EcoLoopSettings) -> None:
        df = make_df(
            [
                {
                    "clock_iso": "2020-01-01T00:00:00",
                    "zone__CORE_ZN__occupancy_fraction": 0.0,
                    "zone__CORE_ZN__pmv": 2.5,
                    "zone__CORE_ZN__ppd_pct": 90.0,
                }
            ]
        )

        metrics = compute_comfort_metrics(df, settings)

        assert metrics.occupied_zone_timesteps == 0
        assert metrics.violation_fraction is None

    def test_none_pmv_excluded_not_scored_as_comfortable(self, settings: EcoLoopSettings) -> None:
        """A zone this run cannot see PMV for must not count as comfortable."""
        df = make_df(
            [
                {
                    "clock_iso": "2020-01-01T00:00:00",
                    "zone__ATTIC__occupancy_fraction": 0.9,
                    "zone__ATTIC__pmv": None,
                    "zone__ATTIC__ppd_pct": None,
                }
            ]
        )

        metrics = compute_comfort_metrics(df, settings)

        assert metrics.occupied_zone_timesteps == 0

    def test_out_of_band_pmv_counts_as_a_violation(self, settings: EcoLoopSettings) -> None:
        df = make_df(
            [
                {
                    "clock_iso": "2020-01-01T00:00:00",
                    "zone__CORE_ZN__occupancy_fraction": 0.8,
                    "zone__CORE_ZN__pmv": 1.2,
                    "zone__CORE_ZN__ppd_pct": 35.0,
                },
                {
                    "clock_iso": "2020-01-01T00:15:00",
                    "zone__CORE_ZN__occupancy_fraction": 0.8,
                    "zone__CORE_ZN__pmv": 0.1,
                    "zone__CORE_ZN__ppd_pct": 5.0,
                },
            ]
        )

        metrics = compute_comfort_metrics(df, settings)

        assert metrics.occupied_zone_timesteps == 2
        assert metrics.violation_zone_timesteps == 1
        assert metrics.violation_fraction == pytest.approx(0.5)
        assert metrics.max_abs_pmv == pytest.approx(1.2)
        assert metrics.unmet_hours == pytest.approx(0.25)  # one violation * 15-minute timestep

    def test_multiple_zones_are_aggregated(self, settings: EcoLoopSettings) -> None:
        df = make_df(
            [
                {
                    "clock_iso": "2020-01-01T00:00:00",
                    "zone__CORE_ZN__occupancy_fraction": 0.8,
                    "zone__CORE_ZN__pmv": 0.1,
                    "zone__PERIMETER_ZN_1__occupancy_fraction": 0.8,
                    "zone__PERIMETER_ZN_1__pmv": -0.9,
                },
            ]
        )

        metrics = compute_comfort_metrics(df, settings)

        assert metrics.occupied_zone_timesteps == 2
        assert metrics.violation_zone_timesteps == 1
