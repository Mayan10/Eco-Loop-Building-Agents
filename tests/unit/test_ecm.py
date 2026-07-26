"""Tests for individual energy-conservation measures."""

from __future__ import annotations

from ecoloop.config import RuleBasedSettings
from ecoloop.control.ecm import economiser_shift, unoccupied_setback

SETTINGS = RuleBasedSettings(
    setback_enabled=True,
    deadband_widening_unoccupied_c=2.0,
    economiser_enabled=True,
    economiser_max_oat_c=18.0,
    economiser_min_oat_c=4.0,
    economiser_setpoint_shift_c=1.0,
)


class TestUnoccupiedSetback:
    def test_occupied_zone_is_unchanged(self) -> None:
        assert unoccupied_setback(21.0, 24.0, occupied=True, settings=SETTINGS) == (21.0, 24.0)

    def test_unoccupied_zone_is_widened_symmetrically(self) -> None:
        heating, cooling = unoccupied_setback(21.0, 24.0, occupied=False, settings=SETTINGS)
        assert heating == 20.0  # -1.0 (half of 2.0)
        assert cooling == 25.0  # +1.0

    def test_disabled_measure_is_a_no_op_even_when_unoccupied(self) -> None:
        disabled = SETTINGS.model_copy(update={"setback_enabled": False})
        assert unoccupied_setback(21.0, 24.0, occupied=False, settings=disabled) == (21.0, 24.0)


class TestEconomiserShift:
    def test_within_band_lowers_cooling_only(self) -> None:
        heating, cooling = economiser_shift(
            21.0, 24.0, outdoor_air_temperature_c=10.0, settings=SETTINGS
        )
        assert heating == 21.0
        assert cooling == 23.0

    def test_outside_band_is_unchanged(self) -> None:
        assert economiser_shift(21.0, 24.0, outdoor_air_temperature_c=30.0, settings=SETTINGS) == (
            21.0,
            24.0,
        )

    def test_at_band_boundary_is_included(self) -> None:
        _, cooling = economiser_shift(
            21.0, 24.0, outdoor_air_temperature_c=SETTINGS.economiser_max_oat_c, settings=SETTINGS
        )
        assert cooling == 23.0

    def test_disabled_measure_is_a_no_op(self) -> None:
        disabled = SETTINGS.model_copy(update={"economiser_enabled": False})
        assert economiser_shift(21.0, 24.0, outdoor_air_temperature_c=10.0, settings=disabled) == (
            21.0,
            24.0,
        )
