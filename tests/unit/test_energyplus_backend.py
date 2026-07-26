"""Tests for the real EnergyPlusBackend adapter.

Needs a real EnergyPlus installation to construct a state, so the whole
module is marked ``energyplus`` and deselected by CI. No simulation is
actually run: these poke at the adapter directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ecoloop.config import load_settings
from ecoloop.simulation.energyplus import EnergyPlusBackend
from ecoloop.simulation.locate import discover_energyplus

pytestmark = pytest.mark.energyplus


@pytest.fixture
def backend() -> EnergyPlusBackend:
    settings = load_settings(profile="fast")
    install = discover_energyplus(settings.simulation.energyplus_dir)
    return EnergyPlusBackend(install)


class TestClockRollover:
    """Regression test: EnergyPlus's minutes() has been observed to return
    values past 60 near environment boundaries (65, on the last timestep of
    a real annual run). Constructing SimClock from raw field values crashed
    with a Pydantic validation error deep inside the reflex callback; clock()
    must roll any such overflow into the day/month/year instead.
    """

    def test_minute_past_60_rolls_into_next_hour(self, backend: EnergyPlusBackend) -> None:
        """The exact values observed on the last timestep of a real run:
        raw hour 23 (-> 22) with minute 65 is 23:05, not a crash."""
        backend._api.exchange = SimpleNamespace(
            year=lambda _s: 1981,
            month=lambda _s: 12,
            day_of_month=lambda _s: 31,
            hour=lambda _s: 23,  # EnergyPlus 1-24 scale
            minutes=lambda _s: 65,
            day_of_week=lambda _s: 1,
        )
        year, month, day, hour, minute, _ = backend.clock()
        assert (year, month, day, hour, minute) == (1981, 12, 31, 23, 5)

    def test_overflow_at_year_end_rolls_into_next_year(self, backend: EnergyPlusBackend) -> None:
        backend._api.exchange = SimpleNamespace(
            year=lambda _s: 1981,
            month=lambda _s: 12,
            day_of_month=lambda _s: 31,
            hour=lambda _s: 24,  # EnergyPlus 1-24 scale -> 23
            minutes=lambda _s: 60,
            day_of_week=lambda _s: 1,
        )
        year, month, day, hour, minute, _ = backend.clock()
        assert (year, month, day, hour, minute) == (1982, 1, 1, 0, 0)

    def test_ordinary_minute_is_unaffected(self, backend: EnergyPlusBackend) -> None:
        backend._api.exchange = SimpleNamespace(
            year=lambda _s: 1999,
            month=lambda _s: 6,
            day_of_month=lambda _s: 15,
            hour=lambda _s: 14,
            minutes=lambda _s: 30,
            day_of_week=lambda _s: 3,
        )
        year, month, day, hour, minute, day_of_week = backend.clock()
        assert (year, month, day, hour, minute, day_of_week) == (1999, 6, 15, 13, 30, 3)

    def test_minute_exactly_60_rolls_to_the_next_hour(self, backend: EnergyPlusBackend) -> None:
        backend._api.exchange = SimpleNamespace(
            year=lambda _s: 1999,
            month=lambda _s: 6,
            day_of_month=lambda _s: 15,
            hour=lambda _s: 14,
            minutes=lambda _s: 60,
            day_of_week=lambda _s: 3,
        )
        year, month, day, hour, minute, _ = backend.clock()
        assert (year, month, day, hour, minute) == (1999, 6, 15, 14, 0)
