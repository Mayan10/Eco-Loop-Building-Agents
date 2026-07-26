"""ASHRAE 55 comfort scoring from a run's persisted telemetry history.

Scored against occupied, conditioned zones only: an unconditioned zone (the
attic) has no PMV output at all, and an unoccupied zone's PMV excursion is
not a comfort failure — nobody is there to feel it. A zone-timestep with a
``None`` PMV (this run's People object never asked for the Fanger output) is
excluded from both the numerator and denominator, never silently counted as
comfortable — the same "measurement absence is not measurement zero"
discipline :class:`~ecoloop.bus.models.ZoneTelemetry` documents.
"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict

from ecoloop.config import EcoLoopSettings

__all__ = ["ComfortMetrics", "compute_comfort_metrics"]

_OCCUPANCY_SUFFIX = "__occupancy_fraction"
_PMV_SUFFIX = "__pmv"
_PPD_SUFFIX = "__ppd_pct"


class ComfortMetrics(BaseModel):
    """ASHRAE 55 comfort scoring for one run's occupied, conditioned zones."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occupied_zone_timesteps: int
    """Zone-timesteps where the zone was occupied and PMV was measurable."""
    violation_zone_timesteps: int
    """Of those, how many had ``|PMV|`` outside ``comfort.pmv_occupied_*``."""
    violation_fraction: float | None
    """``None`` when there were no scorable occupied zone-timesteps at all."""
    max_abs_pmv: float | None
    mean_ppd_pct: float | None
    unmet_hours: float
    """Occupied-and-violating zone-timesteps converted to hours, using the
    run's own median timestep duration rather than an assumed constant."""


def compute_comfort_metrics(df: pd.DataFrame, settings: EcoLoopSettings) -> ComfortMetrics:
    """Score a run's occupied, conditioned zones against ASHRAE 55.

    Args:
        df: A run's telemetry, as read by
            :func:`ecoloop.analysis.collect.read_telemetry`.
        settings: Loaded Eco-Loop settings, for ``comfort.*`` thresholds.

    Returns:
        Comfort metrics aggregated across every conditioned zone and timestep.
    """
    comfort = settings.comfort
    zone_prefixes = _zone_prefixes_with_pmv(df)

    occupied_pmv_frames = []
    occupied_ppd_frames = []
    for prefix in zone_prefixes:
        occupancy = df[f"{prefix}{_OCCUPANCY_SUFFIX}"]
        pmv = df[f"{prefix}{_PMV_SUFFIX}"]
        is_occupied = occupancy > comfort.occupied_threshold_fraction
        has_pmv = pmv.notna()
        mask = is_occupied & has_pmv
        occupied_pmv_frames.append(pmv[mask])
        if f"{prefix}{_PPD_SUFFIX}" in df.columns:
            ppd = df[f"{prefix}{_PPD_SUFFIX}"]
            occupied_ppd_frames.append(ppd[mask & ppd.notna()])

    empty: pd.Series = pd.Series([], dtype=float)
    non_empty_pmv = [s for s in occupied_pmv_frames if not s.empty]
    non_empty_ppd = [s for s in occupied_ppd_frames if not s.empty]
    occupied_pmv = pd.concat(non_empty_pmv) if non_empty_pmv else empty
    occupied_ppd = pd.concat(non_empty_ppd) if non_empty_ppd else empty

    below_band = occupied_pmv < comfort.pmv_occupied_min
    above_band = occupied_pmv > comfort.pmv_occupied_max
    violations = below_band | above_band
    occupied_count = len(occupied_pmv)
    violation_count = int(violations.sum())

    timestep_hours = _median_timestep_hours(df)

    return ComfortMetrics(
        occupied_zone_timesteps=occupied_count,
        violation_zone_timesteps=violation_count,
        violation_fraction=(violation_count / occupied_count) if occupied_count else None,
        max_abs_pmv=float(occupied_pmv.abs().max()) if occupied_count else None,
        mean_ppd_pct=float(occupied_ppd.mean()) if len(occupied_ppd) else None,
        unmet_hours=violation_count * timestep_hours,
    )


def _zone_prefixes_with_pmv(df: pd.DataFrame) -> list[str]:
    """Every ``zone__<NAME>`` prefix that has both occupancy and PMV columns."""
    prefixes = []
    for column in df.columns:
        if not column.endswith(_OCCUPANCY_SUFFIX):
            continue
        prefix = column[: -len(_OCCUPANCY_SUFFIX)]
        if f"{prefix}{_PMV_SUFFIX}" in df.columns:
            prefixes.append(prefix)
    return prefixes


def _median_timestep_hours(df: pd.DataFrame) -> float:
    """The run's typical timestep duration in hours, from its own clock column.

    Derived from the data rather than assumed, since the active profile's
    timestep length is a config choice this module should not have to know
    about separately (AGENTS.md invariant #8: no number hard-coded in ``src/``).
    """
    if "clock_iso" not in df.columns or len(df) < 2:
        return 0.0
    clocks = pd.to_datetime(df["clock_iso"])
    deltas = clocks.diff().dropna()
    if deltas.empty:
        return 0.0
    return float(deltas.median().total_seconds() / 3600.0)
