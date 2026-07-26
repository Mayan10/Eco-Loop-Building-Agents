#!/usr/bin/env python3
"""Generate the synthetic grid carbon-intensity and time-of-use tariff signals.

**Provenance.** These are *synthetic but realistic* profiles, not measured data.
They are shipped in-repo deliberately: the judging environment may be offline, so
depending on a live grid API would be a single point of failure for the whole
demonstration. Live fetching exists as an optional adapter, disabled by default
(``signals.live_fetch_enabled``).

The shapes are modelled on the well-documented "duck curve" seen in grids with
significant solar penetration (CAISO, and increasingly the Indian Southern Grid):

* **Carbon intensity** — a midday trough as utility-scale solar displaces
  thermal generation, and a sharp evening peak as solar falls off while demand
  is still high and peaking plant comes online. Overnight sits at a moderate
  baseload level dominated by coal and combined-cycle gas.
* **Tariff** — a three-tier time-of-use structure (off-peak / shoulder / peak)
  of the kind found in Indian commercial tariffs and US demand-response
  programmes, with the peak deliberately *offset* from the carbon peak.

That offset matters. If price and carbon peaked together, "cheap" and "clean"
would be the same objective and the agent's multi-objective reasoning would be
untestable. Because they diverge, the agent must genuinely trade cost against
emissions, which is the behaviour we want to observe and report.

Regenerate with:

    python scripts/generate_signals.py
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIGNALS_DIR = PROJECT_ROOT / "config" / "signals"

HOURS_PER_DAY = 24

# --- Carbon intensity (gCO2/kWh) ------------------------------------------- #
# Baseload from thermal plant, before solar displacement or evening peaking.
CARBON_BASELOAD = 480.0
# Depth of the midday solar trough.
CARBON_SOLAR_DEPTH = 210.0
# Solar generation window and its peak hour.
SOLAR_START_HOUR, SOLAR_END_HOUR, SOLAR_PEAK_HOUR = 7.0, 18.0, 12.5
# Evening peaking-plant surcharge: magnitude, centre hour, and spread.
CARBON_EVENING_PEAK = 120.0
CARBON_EVENING_HOUR, CARBON_EVENING_WIDTH = 19.5, 2.2

# --- Time-of-use tariff (currency/kWh) ------------------------------------- #
# Indicative Indian commercial rates in INR/kWh; units are configurable and the
# analysis layer treats them as an abstract currency.
TARIFF_OFF_PEAK = 4.50
TARIFF_SHOULDER = 7.20
TARIFF_PEAK = 11.80
# Peak billing window, offset from the carbon peak on purpose (see module docs).
PEAK_HOURS = frozenset({9, 10, 11, 17, 18, 19, 20})
SHOULDER_HOURS = frozenset({7, 8, 12, 13, 14, 15, 16, 21})


def carbon_intensity(hour: int) -> float:
    """Return grid carbon intensity for an hour of the day.

    Args:
        hour: Hour of day, 0-23.

    Returns:
        Carbon intensity in gCO2 per kWh, rounded to one decimal.
    """
    intensity = CARBON_BASELOAD

    # Half-sine solar displacement across the generation window.
    if SOLAR_START_HOUR <= hour <= SOLAR_END_HOUR:
        phase = (hour - SOLAR_START_HOUR) / (SOLAR_END_HOUR - SOLAR_START_HOUR)
        intensity -= CARBON_SOLAR_DEPTH * math.sin(math.pi * phase)

    # Gaussian evening peaking-plant surcharge.
    exponent = -(((hour - CARBON_EVENING_HOUR) / CARBON_EVENING_WIDTH) ** 2)
    intensity += CARBON_EVENING_PEAK * math.exp(exponent)

    return round(intensity, 1)


def tariff(hour: int) -> float:
    """Return the time-of-use energy price for an hour of the day.

    Args:
        hour: Hour of day, 0-23.

    Returns:
        Price per kWh in the configured currency.
    """
    if hour in PEAK_HOURS:
        return TARIFF_PEAK
    if hour in SHOULDER_HOURS:
        return TARIFF_SHOULDER
    return TARIFF_OFF_PEAK


def tariff_period(hour: int) -> str:
    """Return the human-readable billing period label for an hour.

    Args:
        hour: Hour of day, 0-23.

    Returns:
        One of ``peak``, ``shoulder``, or ``off_peak``.
    """
    if hour in PEAK_HOURS:
        return "peak"
    if hour in SHOULDER_HOURS:
        return "shoulder"
    return "off_peak"


def write_carbon(path: Path) -> None:
    """Write the hourly carbon-intensity signal.

    Args:
        path: Destination CSV path.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hour", "gco2_per_kwh"])
        writer.writerows([hour, carbon_intensity(hour)] for hour in range(HOURS_PER_DAY))


def write_tariff(path: Path) -> None:
    """Write the hourly time-of-use tariff signal.

    Args:
        path: Destination CSV path.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hour", "price_per_kwh", "period"])
        writer.writerows(
            [hour, f"{tariff(hour):.2f}", tariff_period(hour)] for hour in range(HOURS_PER_DAY)
        )


def main() -> None:
    """Regenerate both signal files and print a summary."""
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    carbon_path = SIGNALS_DIR / "carbon_intensity_hourly.csv"
    tariff_path = SIGNALS_DIR / "tariff_tou.csv"

    write_carbon(carbon_path)
    write_tariff(tariff_path)

    values = [carbon_intensity(hour) for hour in range(HOURS_PER_DAY)]
    cleanest = min(range(HOURS_PER_DAY), key=lambda h: values[h])
    dirtiest = max(range(HOURS_PER_DAY), key=lambda h: values[h])

    print(f"wrote {carbon_path.relative_to(PROJECT_ROOT)}")
    print(f"wrote {tariff_path.relative_to(PROJECT_ROOT)}")
    print(
        f"  carbon: {min(values):.0f}-{max(values):.0f} gCO2/kWh "
        f"(cleanest {cleanest:02d}:00, dirtiest {dirtiest:02d}:00)"
    )
    print(
        f"  tariff: {TARIFF_OFF_PEAK:.2f} off-peak / {TARIFF_SHOULDER:.2f} shoulder / "
        f"{TARIFF_PEAK:.2f} peak per kWh"
    )


if __name__ == "__main__":
    main()
