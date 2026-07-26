"""Flatten :class:`TelemetrySample` streams into a tabular per-run history.

Nothing upstream of this module retains a full run's telemetry: the live
:class:`~ecoloop.bus.telemetry.TelemetryBus` is a bounded, drop-oldest ring
buffer by design (it exists for the cognitive tier's rolling-window reads,
not as a durable record — see AGENTS.md §12), and EnergyPlus's own
``.eso``/``.mtr`` output uses a different variable vocabulary and reporting
frequency than the runtime data-exchange point map in ``config/zones.yaml``.
So a post-run comparison report has exactly one honest source of truth: the
same :class:`~ecoloop.bus.models.TelemetrySample` objects the reflex tier
already builds every timestep, collected as they are produced and written to
disk once the run completes.

Rows are buffered in memory during the run — never written to disk from
inside the EnergyPlus callback, which would be I/O in a synchronous physics
callback (AGENTS.md invariant #1/#6 territory) — and flushed in one batch
afterward.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ecoloop.bus.models import TelemetrySample

__all__ = ["TelemetryRecorder", "flatten_sample", "read_telemetry", "write_telemetry"]


def flatten_sample(sample: TelemetrySample) -> dict[str, Any]:
    """Flatten one :class:`TelemetrySample` into a single wide row.

    Args:
        sample: A non-warmup telemetry sample.

    Returns:
        A flat ``dict`` suitable for a single :class:`pandas.DataFrame` row:
        clock/environment fields, ``site__*`` columns, ``meter__*`` columns
        keyed by meter name, and ``zone__<ZONE>__*`` columns per zone field.
    """
    row: dict[str, Any] = {
        "clock_iso": sample.clock.isoformat(),
        "timestep_index": sample.timestep_index,
        "environment": sample.environment,
        "site__outdoor_air_temperature_c": sample.site.outdoor_air_temperature_c,
        "site__outdoor_relative_humidity_pct": sample.site.outdoor_relative_humidity_pct,
        "site__direct_normal_radiation_w_m2": sample.site.direct_normal_radiation_w_m2,
        "site__diffuse_horizontal_radiation_w_m2": sample.site.diffuse_horizontal_radiation_w_m2,
        "site__wind_speed_m_s": sample.site.wind_speed_m_s,
    }
    for meter in sample.meters:
        row[f"meter__{meter.name}_j"] = meter.joules
    for zone in sample.zones:
        prefix = f"zone__{zone.zone}"
        row[f"{prefix}__air_temperature_c"] = zone.air_temperature_c
        row[f"{prefix}__relative_humidity_pct"] = zone.relative_humidity_pct
        row[f"{prefix}__heating_setpoint_c"] = zone.heating_setpoint_c
        row[f"{prefix}__cooling_setpoint_c"] = zone.cooling_setpoint_c
        row[f"{prefix}__occupancy_fraction"] = zone.occupancy_fraction
        row[f"{prefix}__pmv"] = zone.pmv
        row[f"{prefix}__ppd_pct"] = zone.ppd_pct
        row[f"{prefix}__co2_ppm"] = zone.co2_ppm
    return row


class TelemetryRecorder:
    """Buffers flattened rows in memory for the duration of a run.

    Pass :meth:`record` as (or wrap it in) a run's ``on_sample`` callback
    alongside whatever publishes to the live ``TelemetryBus`` — see
    :mod:`ecoloop.runner`.
    """

    def __init__(self) -> None:
        """Create an empty recorder with no buffered rows."""
        self._rows: list[dict[str, Any]] = []

    def record(self, sample: TelemetrySample) -> None:
        """Flatten and buffer one sample. Cheap: no I/O, just a dict + append."""
        self._rows.append(flatten_sample(sample))

    def __len__(self) -> int:
        """The number of rows buffered so far."""
        return len(self._rows)

    def to_dataframe(self) -> pd.DataFrame:
        """Return every buffered row as a :class:`pandas.DataFrame`."""
        return pd.DataFrame.from_records(self._rows)

    def write(self, path: Path) -> None:
        """Write every buffered row to ``path`` as Parquet.

        Args:
            path: Destination file. Parent directories are created if needed.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.to_dataframe().to_parquet(path, index=False)


def write_telemetry(df: pd.DataFrame, path: Path) -> None:
    """Write a telemetry :class:`~pandas.DataFrame` to ``path`` as Parquet.

    Args:
        df: A frame produced by :meth:`TelemetryRecorder.to_dataframe`.
        path: Destination file. Parent directories are created if needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def read_telemetry(path: Path) -> pd.DataFrame:
    """Read a run's persisted telemetry back from Parquet.

    Args:
        path: A file previously written by :func:`write_telemetry` or
            :meth:`TelemetryRecorder.write`.

    Returns:
        The run's full per-timestep history.
    """
    return pd.read_parquet(path)
