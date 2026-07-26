"""Read the synthetic grid carbon-intensity and tariff signals.

Both CSVs are tiny (24 rows, one per hour of day) and rarely read — parsing
fresh on every call is simpler than caching and costs nothing measurable.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ecoloop.errors import ConfigError

__all__ = ["read_carbon_intensity", "read_tariff"]


def read_carbon_intensity(path: Path, hour_of_day: int) -> float:
    """Read the carbon intensity for one hour of day.

    Args:
        path: Path to ``carbon_intensity_hourly.csv``.
        hour_of_day: Hour, 0-23 (wrapped with ``% 24`` by the caller for
            forecast hours that cross midnight).

    Returns:
        Grid carbon intensity in gCO2/kWh.

    Raises:
        ConfigError: If the file is missing or has no row for that hour.
    """
    return _read_hourly_csv(path, hour_of_day, value_column="gco2_per_kwh")


def read_tariff(path: Path, hour_of_day: int) -> float:
    """Read the time-of-use tariff for one hour of day.

    Args:
        path: Path to ``tariff_tou.csv``.
        hour_of_day: Hour, 0-23.

    Returns:
        Price per kWh, in the configured currency.

    Raises:
        ConfigError: If the file is missing or has no row for that hour.
    """
    return _read_hourly_csv(path, hour_of_day, value_column="price_per_kwh")


def _read_hourly_csv(path: Path, hour_of_day: int, *, value_column: str) -> float:
    """Shared reader for the two hour-indexed signal CSVs.

    Args:
        path: CSV path.
        hour_of_day: Hour, 0-23.
        value_column: Which column holds the numeric value.

    Returns:
        The value for that hour.

    Raises:
        ConfigError: If the file is missing or has no row for that hour.
    """
    if not path.is_file():
        raise ConfigError("signal CSV not found", path=str(path))
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if int(row["hour"]) == hour_of_day:
                return float(row[value_column])
    raise ConfigError("signal CSV has no row for hour", path=str(path), hour=hour_of_day)
