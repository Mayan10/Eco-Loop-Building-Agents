"""Tests for EPW weather file parsing and the forecast oracle.

Fixtures are small synthetic EPW files built in ``tmp_path`` rather than a real
weather file, so parsing edge cases (missing-value sentinels, malformed rows,
year wraparound) can be constructed deliberately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.errors import ConfigError
from ecoloop.simulation.weather import load_epw

_HEADER = [
    "LOCATION,Chicago Ohare Intl Ap,IL,USA,TMY3,725300,41.98,-87.92,-6.0,201.0",
    "DESIGN CONDITIONS,1,Climate Design Data 2009 ASHRAE Handbook",
    "TYPICAL/EXTREME PERIODS,0",
    "GROUND TEMPERATURES,0",
    "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0",
    "COMMENTS 1,synthetic test fixture",
    "COMMENTS 2,",
    "DATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31",
]


def _row(
    month: int,
    day: int,
    hour: int,
    *,
    dry_bulb: float = 10.0,
    dew_point: float = 5.0,
    rh: float = 60.0,
    ghr: float = 200.0,
    dnr: float = 100.0,
    dhr: float = 50.0,
    wind: float = 3.0,
) -> str:
    """Build one EPW data row with the 22 columns this module reads."""
    fields = [
        "1999",
        str(month),
        str(day),
        str(hour),
        "0",
        "?9?9?9?9E0?9?9?9?9?9?9?9?9?9?9?9?9?9",
        f"{dry_bulb}",
        f"{dew_point}",
        f"{rh}",
        "99999",  # atmospheric pressure, unused
        "0",  # extraterrestrial horizontal
        "0",  # extraterrestrial direct normal
        "0",  # horizontal infrared sky
        f"{ghr}",
        f"{dnr}",
        f"{dhr}",
        "0",
        "0",
        "0",
        "0",
        "0",  # wind direction, unused
        f"{wind}",
    ]
    return ",".join(fields)


def write_epw(tmp_path: Path, rows: list[str], *, header: list[str] | None = None) -> Path:
    path = tmp_path / "test.epw"
    lines = (header if header is not None else _HEADER) + rows
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def two_days(tmp_path: Path) -> Path:
    rows = [_row(1, 1, h) for h in range(1, 25)] + [_row(1, 2, h) for h in range(1, 25)]
    return write_epw(tmp_path, rows)


class TestHeaderParsing:
    def test_location_fields_are_parsed(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        assert weather.city == "Chicago Ohare Intl Ap"
        assert weather.state_province == "IL"
        assert weather.country == "USA"
        assert weather.latitude == pytest.approx(41.98)
        assert weather.longitude == pytest.approx(-87.92)
        assert weather.timezone_offset_hours == pytest.approx(-6.0)
        assert weather.elevation_m == pytest.approx(201.0)

    def test_missing_location_header_raises(self, tmp_path: Path) -> None:
        bad_header = ["NOT A LOCATION LINE", *_HEADER[1:]]
        with pytest.raises(ConfigError, match="LOCATION"):
            load_epw(write_epw(tmp_path, [_row(1, 1, 1)], header=bad_header))

    def test_file_with_no_data_rows_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no data rows"):
            load_epw(write_epw(tmp_path, []))


class TestDataParsing:
    def test_row_count_matches_input(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        assert len(weather.hours) == 48

    def test_values_round_trip(self, tmp_path: Path) -> None:
        rows = [_row(6, 15, 14, dry_bulb=28.4, wind=4.2)]
        weather = load_epw(write_epw(tmp_path, rows))
        hour = weather.hours[0]
        assert hour.dry_bulb_c == pytest.approx(28.4)
        assert hour.wind_speed_m_s == pytest.approx(4.2)
        assert hour.hour == 14

    def test_malformed_row_raises(self, tmp_path: Path) -> None:
        rows = [_row(1, 1, 1, dry_bulb=10.0).replace("10.0", "not-a-number")]
        with pytest.raises(ConfigError, match="non-numeric"):
            load_epw(write_epw(tmp_path, rows))

    def test_short_row_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="too few columns"):
            load_epw(write_epw(tmp_path, ["1999,1,1,1,0"]))

    def test_blank_lines_between_rows_are_skipped(self, tmp_path: Path) -> None:
        rows = [_row(1, 1, 1), "", _row(1, 1, 2)]
        weather = load_epw(write_epw(tmp_path, rows))
        assert len(weather.hours) == 2


class TestMissingValueSentinels:
    def test_temperature_sentinel_becomes_none(self, tmp_path: Path) -> None:
        rows = [_row(1, 1, 1, dry_bulb=99.9, dew_point=99.9)]
        weather = load_epw(write_epw(tmp_path, rows))
        assert weather.hours[0].dry_bulb_c is None
        assert weather.hours[0].dew_point_c is None

    def test_radiation_sentinel_becomes_none(self, tmp_path: Path) -> None:
        rows = [_row(1, 1, 1, ghr=9999, dnr=9999, dhr=9999)]
        weather = load_epw(write_epw(tmp_path, rows))
        hour = weather.hours[0]
        assert hour.global_horizontal_wh_m2 is None
        assert hour.direct_normal_wh_m2 is None
        assert hour.diffuse_horizontal_wh_m2 is None

    def test_wind_sentinel_becomes_none(self, tmp_path: Path) -> None:
        rows = [_row(1, 1, 1, wind=999)]
        weather = load_epw(write_epw(tmp_path, rows))
        assert weather.hours[0].wind_speed_m_s is None

    def test_real_zero_is_not_treated_as_missing(self, tmp_path: Path) -> None:
        rows = [_row(1, 1, 1, wind=0.0, ghr=0.0)]
        weather = load_epw(write_epw(tmp_path, rows))
        assert weather.hours[0].wind_speed_m_s == 0.0
        assert weather.hours[0].global_horizontal_wh_m2 == 0.0


class TestForecastOracle:
    def test_hour_index_locates_exact_row(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        assert weather.hour_index(1, 2, 1) == 24

    def test_unknown_date_raises(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        with pytest.raises(ConfigError, match="not found"):
            weather.hour_index(3, 1, 1)

    def test_forecast_reads_ahead_in_chronological_order(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        forecast = weather.forecast(1, 1, 1, horizon_hours=3)
        assert [h.hour for h in forecast] == [1, 2, 3]
        assert [h.day for h in forecast] == [1, 1, 1]

    def test_forecast_wraps_past_end_of_series(self, tmp_path: Path) -> None:
        """Requesting a forecast at the last hour must not raise or truncate."""
        weather = load_epw(two_days(tmp_path))
        forecast = weather.forecast(1, 2, 24, horizon_hours=3)
        assert [(h.day, h.hour) for h in forecast] == [(2, 24), (1, 1), (1, 2)]

    def test_forecast_longer_than_series_is_capped(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        forecast = weather.forecast(1, 1, 1, horizon_hours=1000)
        assert len(forecast) == 48


class TestSummary:
    def test_summary_reports_site_and_hour_count(self, tmp_path: Path) -> None:
        weather = load_epw(two_days(tmp_path))
        summary = weather.summary()
        assert summary["city"] == "Chicago Ohare Intl Ap"
        assert summary["hours_parsed"] == 48
