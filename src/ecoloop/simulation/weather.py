"""Read an EPW weather file and serve the disclosed forecast oracle.

EPW ("EnergyPlus Weather") files carry eight header lines followed by one row
per hour of a typical year — 8760 rows for a non-leap TMY file. This module
parses the header for site metadata and the data rows into typed,
missing-value-aware hourly records.

**The forecast oracle.** :meth:`WeatherFile.forecast` reads *ahead* in the
file relative to the simulation's current position. This is a deliberate
design choice, disclosed here and in ``docs/ARCHITECTURE.md``: the cognitive
layer is allowed to condition on a short-horizon weather forecast because a
real deployment would have one (a commercial forecast API), and simulating
that with perfect foresight into the same EPW is simpler than fabricating a
noisy forecast model. It is not a data leak bug — but it is a capability the
agent has that the *baseline* and *rule-based* controllers deliberately do not
use, and any comparison across controllers must account for that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from ecoloop.errors import ConfigError
from ecoloop.logging import get_logger

__all__ = [
    "WeatherFile",
    "WeatherHour",
    "load_epw",
]

_logger = get_logger(__name__, component="simulation")

_HEADER_LINE_COUNT: Final = 8
_HOURS_PER_DAY: Final = 24

# 0-based column indices in an EPW data row (EPW Auxiliary Programs spec).
_COL_YEAR: Final = 0
_COL_MONTH: Final = 1
_COL_DAY: Final = 2
_COL_HOUR: Final = 3
_COL_DRY_BULB: Final = 6
_COL_DEW_POINT: Final = 7
_COL_RELATIVE_HUMIDITY: Final = 8
_COL_GLOBAL_HORIZONTAL: Final = 13
_COL_DIRECT_NORMAL: Final = 14
_COL_DIFFUSE_HORIZONTAL: Final = 15
_COL_WIND_SPEED: Final = 21
_MIN_COLUMNS: Final = _COL_WIND_SPEED + 1

# Missing-value sentinels per the EPW spec. A reading equal to its sentinel is
# not a real measurement and must not be propagated as one.
_MISSING_TEMPERATURE: Final = 99.9
_MISSING_RELATIVE_HUMIDITY: Final = 999.0
_MISSING_RADIATION: Final = 9999.0
_MISSING_WIND_SPEED: Final = 999.0


def _clean(value: float, sentinel: float) -> float | None:
    """Return ``value`` unless it equals the EPW missing-data sentinel.

    Args:
        value: Parsed field value.
        sentinel: The EPW missing-value code for this field.

    Returns:
        ``value``, or ``None`` if it is the sentinel.
    """
    return None if value == sentinel else value


class WeatherHour(BaseModel):
    """One hour of an EPW weather series.

    Any field is ``None`` when the source file reports the EPW missing-value
    sentinel for it (``99.9`` for temperatures, ``9999`` for radiation, and so
    on) — treated as absent, not as zero.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    month: int
    day: int
    hour: int
    """1-24, matching the EPW convention rather than a 0-23 clock hour."""
    dry_bulb_c: float | None
    dew_point_c: float | None
    relative_humidity_pct: float | None
    global_horizontal_wh_m2: float | None
    direct_normal_wh_m2: float | None
    diffuse_horizontal_wh_m2: float | None
    wind_speed_m_s: float | None


class WeatherFile(BaseModel):
    """A parsed EPW file: site metadata plus its full hourly series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    city: str
    state_province: str
    country: str
    latitude: float
    longitude: float
    timezone_offset_hours: float
    elevation_m: float
    hours: tuple[WeatherHour, ...]

    def hour_index(self, month: int, day: int, hour: int) -> int:
        """Locate an hour's position in the parsed series.

        Args:
            month: Calendar month, 1-12.
            day: Day of month.
            hour: EPW hour-of-day, 1-24.

        Returns:
            The 0-based index into :attr:`hours`.

        Raises:
            ConfigError: If no row matches — the series does not cover a full
                year, or the date does not exist in it.
        """
        for index, row in enumerate(self.hours):
            if row.month == month and row.day == day and row.hour == hour:
                return index
        raise ConfigError(
            "date not found in weather file",
            path=str(self.path),
            month=month,
            day=day,
            hour=hour,
        )

    def forecast(
        self, month: int, day: int, hour: int, horizon_hours: int
    ) -> tuple[WeatherHour, ...]:
        """Read ahead in the weather file from a given hour.

        This is the disclosed forecast oracle described in the module
        docstring: it looks past the simulation's current position, which only
        the cognitive layer is meant to use.

        Args:
            month: Calendar month, 1-12, of the current simulation time.
            day: Day of month of the current simulation time.
            hour: EPW hour-of-day, 1-24, of the current simulation time.
            horizon_hours: Number of hours to read ahead, inclusive of the
                current hour. Wraps past the end of the series back to its
                start, so a forecast requested for December 31st is served
                from January 1st rather than raising.

        Returns:
            Up to ``horizon_hours`` consecutive hours starting at the given
            time, in chronological order.
        """
        start = self.hour_index(month, day, hour)
        total = len(self.hours)
        span = min(horizon_hours, total)
        return tuple(self.hours[(start + offset) % total] for offset in range(span))

    def summary(self) -> dict[str, object]:
        """A compact description of this weather file for the run manifest.

        Returns:
            Site metadata and the number of hours parsed.
        """
        return {
            "path": str(self.path),
            "city": self.city,
            "state_province": self.state_province,
            "country": self.country,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone_offset_hours": self.timezone_offset_hours,
            "elevation_m": self.elevation_m,
            "hours_parsed": len(self.hours),
        }


def _parse_location(line: str, *, path: Path) -> tuple[str, str, str, float, float, float, float]:
    """Parse the EPW ``LOCATION`` header line.

    Args:
        line: The raw header line, expected to start with ``LOCATION``.
        path: Source path, for error context only.

    Returns:
        ``(city, state_province, country, latitude, longitude,
        timezone_offset_hours, elevation_m)``.

    Raises:
        ConfigError: If the line is missing or malformed.
    """
    fields = line.strip().split(",")
    if len(fields) < 10 or fields[0].strip().upper() != "LOCATION":
        raise ConfigError("EPW file has no valid LOCATION header", path=str(path))
    try:
        return (
            fields[1],
            fields[2],
            fields[3],
            float(fields[6]),
            float(fields[7]),
            float(fields[8]),
            float(fields[9]),
        )
    except ValueError as exc:
        raise ConfigError(
            "EPW LOCATION header has non-numeric lat/long/timezone/elevation",
            path=str(path),
        ) from exc


def _parse_row(fields: list[str], *, line_number: int, path: Path) -> WeatherHour:
    """Parse one EPW data row.

    Args:
        fields: Comma-split row fields.
        line_number: 1-based line number, for error context.
        path: Source path, for error context.

    Returns:
        The parsed hourly record, with sentinel values cleaned to ``None``.

    Raises:
        ConfigError: If the row is too short or contains non-numeric data in a
            field this module reads.
    """
    if len(fields) < _MIN_COLUMNS:
        raise ConfigError(
            "EPW data row has too few columns",
            path=str(path),
            line=line_number,
            columns=len(fields),
        )
    try:
        return WeatherHour(
            month=int(fields[_COL_MONTH]),
            day=int(fields[_COL_DAY]),
            hour=int(fields[_COL_HOUR]),
            dry_bulb_c=_clean(float(fields[_COL_DRY_BULB]), _MISSING_TEMPERATURE),
            dew_point_c=_clean(float(fields[_COL_DEW_POINT]), _MISSING_TEMPERATURE),
            relative_humidity_pct=_clean(
                float(fields[_COL_RELATIVE_HUMIDITY]), _MISSING_RELATIVE_HUMIDITY
            ),
            global_horizontal_wh_m2=_clean(
                float(fields[_COL_GLOBAL_HORIZONTAL]), _MISSING_RADIATION
            ),
            direct_normal_wh_m2=_clean(float(fields[_COL_DIRECT_NORMAL]), _MISSING_RADIATION),
            diffuse_horizontal_wh_m2=_clean(
                float(fields[_COL_DIFFUSE_HORIZONTAL]), _MISSING_RADIATION
            ),
            wind_speed_m_s=_clean(float(fields[_COL_WIND_SPEED]), _MISSING_WIND_SPEED),
        )
    except ValueError as exc:
        raise ConfigError(
            "EPW data row contains non-numeric data",
            path=str(path),
            line=line_number,
        ) from exc


def load_epw(path: Path) -> WeatherFile:
    """Parse an EPW weather file.

    Args:
        path: Path to the ``.epw`` file.

    Returns:
        The parsed weather file, with its full hourly series cached on the
        returned object.

    Raises:
        ConfigError: If the file is missing, has no valid header, or a data
            row cannot be parsed.
    """
    if not path.is_file():
        raise ConfigError("EPW weather file not found", path=str(path))

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) <= _HEADER_LINE_COUNT:
        raise ConfigError("EPW file has no data rows", path=str(path))

    city, state_province, country, latitude, longitude, tz_offset, elevation = _parse_location(
        lines[0], path=path
    )

    hours = tuple(
        _parse_row(line.split(","), line_number=index + 1, path=path)
        for index, line in enumerate(lines[_HEADER_LINE_COUNT:], start=_HEADER_LINE_COUNT)
        if line.strip()
    )

    _logger.info(
        "parsed EPW weather file",
        path=str(path),
        city=city,
        hours_parsed=len(hours),
    )

    return WeatherFile(
        path=path,
        city=city,
        state_province=state_province,
        country=country,
        latitude=latitude,
        longitude=longitude,
        timezone_offset_hours=tz_offset,
        elevation_m=elevation,
        hours=hours,
    )
