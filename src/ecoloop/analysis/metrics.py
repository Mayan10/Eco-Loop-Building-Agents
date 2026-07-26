"""Energy metrics computed from a run's persisted telemetry history.

Conversion from Joules to kWh happens exactly once, here, using
``analysis.joules_per_kwh`` — :class:`~ecoloop.bus.models.MeterReading`
deliberately carries Joules and converts nowhere else, so a unit mistake
cannot exist in two places with two different answers (see
``bus/models.py``'s docstring).

**Total site energy is electricity plus gas, not electricity alone.** This
building's heating plant is ``Coil:Heating:Fuel``, so heating energy lands on
the ``Heating:NaturalGas`` meter — a total that only sums electricity meters
silently drops the entire heating season (AGENTS.md landmine, ``config/zones.yaml``).
"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict

from ecoloop.config import EcoLoopSettings

__all__ = ["EnergyMetrics", "compute_energy_metrics"]

_ELECTRICITY_METER = "ElectricityNet:Facility"
_GAS_METER = "NaturalGas:Facility"
_METER_COLUMN_PREFIX = "meter__"
_METER_COLUMN_SUFFIX = "_j"


class EnergyMetrics(BaseModel):
    """Total and per-meter energy use for one run, in kWh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_kwh: float
    """Electricity plus gas — this building's full site energy use."""
    by_meter_kwh: dict[str, float]
    conditioned_floor_area_m2: float | None
    kwh_per_m2: float | None
    """``None`` when floor area could not be determined for this run."""
    plausible: bool | None
    """Whether ``kwh_per_m2`` falls inside ``analysis.plausible_annual_kwh_per_m2_*``.

    A fast/demo profile run covers less than a year, so this bound is only
    meaningful for an annual (``full``) profile run; callers should not
    flag a fast-profile run as implausible on this basis alone. ``None``
    when ``kwh_per_m2`` itself is ``None``.
    """


def compute_energy_metrics(
    df: pd.DataFrame,
    settings: EcoLoopSettings,
    *,
    conditioned_floor_area_m2: float | None = None,
) -> EnergyMetrics:
    """Compute total and per-meter kWh from a run's telemetry history.

    Args:
        df: A run's telemetry, as read by
            :func:`ecoloop.analysis.collect.read_telemetry`.
        settings: Loaded Eco-Loop settings, for ``analysis.joules_per_kwh``
            and the plausibility bounds.
        conditioned_floor_area_m2: The run's total conditioned floor area,
            from :attr:`~ecoloop.runner.RunManifest.conditioned_floor_area_m2`
            — a per-run fact, not something the telemetry history itself
            carries. ``None`` skips the intensity/plausibility fields.

    Returns:
        Total and per-meter energy metrics for the run.
    """
    meter_columns = [c for c in df.columns if c.startswith(_METER_COLUMN_PREFIX)]
    by_meter_kwh = {
        column.removeprefix(_METER_COLUMN_PREFIX).removesuffix(_METER_COLUMN_SUFFIX): float(
            df[column].sum()
        )
        / settings.analysis.joules_per_kwh
        for column in meter_columns
    }
    total_kwh = by_meter_kwh.get(_ELECTRICITY_METER, 0.0) + by_meter_kwh.get(_GAS_METER, 0.0)

    kwh_per_m2: float | None = None
    plausible: bool | None = None
    if conditioned_floor_area_m2 and conditioned_floor_area_m2 > 0:
        kwh_per_m2 = total_kwh / conditioned_floor_area_m2
        plausible = (
            settings.analysis.plausible_annual_kwh_per_m2_min
            <= kwh_per_m2
            <= settings.analysis.plausible_annual_kwh_per_m2_max
        )

    return EnergyMetrics(
        total_kwh=total_kwh,
        by_meter_kwh=by_meter_kwh,
        conditioned_floor_area_m2=conditioned_floor_area_m2,
        kwh_per_m2=kwh_per_m2,
        plausible=plausible,
    )
