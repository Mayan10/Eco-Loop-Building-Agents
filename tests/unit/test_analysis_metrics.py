"""Tests for energy metrics computed from a run's telemetry history."""

from __future__ import annotations

import pandas as pd
import pytest

from ecoloop.analysis.metrics import compute_energy_metrics
from ecoloop.config import EcoLoopSettings, load_settings


@pytest.fixture
def settings() -> EcoLoopSettings:
    return load_settings(profile="fast")


def make_df(**meter_totals_j: float) -> pd.DataFrame:
    row = {f"meter__{name}_j": value for name, value in meter_totals_j.items()}
    return pd.DataFrame.from_records([row, row])  # two identical rows, sums double


class TestComputeEnergyMetrics:
    def test_total_kwh_is_electricity_plus_gas(self, settings: EcoLoopSettings) -> None:
        df = make_df(**{"ElectricityNet:Facility": 3_600_000.0, "NaturalGas:Facility": 7_200_000.0})

        metrics = compute_energy_metrics(df, settings)

        # Each meter's total is doubled by the two identical rows above.
        assert metrics.total_kwh == pytest.approx(2.0 + 4.0)

    def test_electricity_only_meter_total_omits_gas(self, settings: EcoLoopSettings) -> None:
        """Regression: a total that only sums electricity silently drops the
        entire heating season, since this building heats with natural gas."""
        df = make_df(**{"ElectricityNet:Facility": 3_600_000.0})

        metrics = compute_energy_metrics(df, settings)

        assert metrics.by_meter_kwh.get("NaturalGas:Facility") is None
        assert metrics.total_kwh == pytest.approx(2.0)

    def test_by_meter_kwh_covers_every_meter_column(self, settings: EcoLoopSettings) -> None:
        df = make_df(**{"Cooling:Electricity": 3_600_000.0, "Fans:Electricity": 1_800_000.0})

        metrics = compute_energy_metrics(df, settings)

        assert metrics.by_meter_kwh["Cooling:Electricity"] == pytest.approx(2.0)
        assert metrics.by_meter_kwh["Fans:Electricity"] == pytest.approx(1.0)

    def test_no_floor_area_skips_intensity_and_plausibility(
        self, settings: EcoLoopSettings
    ) -> None:
        df = make_df(**{"ElectricityNet:Facility": 3_600_000.0})

        metrics = compute_energy_metrics(df, settings)

        assert metrics.kwh_per_m2 is None
        assert metrics.plausible is None

    def test_floor_area_yields_intensity_and_plausibility(self, settings: EcoLoopSettings) -> None:
        # 100 kWh/m2 per row, doubled by make_df's two identical rows -> 200,
        # comfortably inside this profile's [0.5, 400] plausible band.
        df = make_df(**{"ElectricityNet:Facility": 3_600_000.0 * 100.0})

        metrics = compute_energy_metrics(df, settings, conditioned_floor_area_m2=1.0)

        assert metrics.kwh_per_m2 == pytest.approx(200.0)
        assert metrics.plausible is True

    def test_implausible_intensity_is_flagged(self, settings: EcoLoopSettings) -> None:
        # 500 kWh/m2 per row, doubled -> 1000 kWh/m2, far above the [0.5, 400] band.
        df = make_df(**{"ElectricityNet:Facility": 3_600_000.0 * 500.0})

        metrics = compute_energy_metrics(df, settings, conditioned_floor_area_m2=1.0)

        assert metrics.plausible is False
