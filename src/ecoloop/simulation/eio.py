"""Parse EnergyPlus's ``.eio`` file for static building facts.

The data-exchange API does not expose these. Total conditioned floor area is
needed to sanity-check a run's energy use
(``analysis.plausible_annual_kwh_per_m2_min/max``), but it is geometry, not a
simulated variable: the IDF's own ``Zone`` objects declare ``Floor Area =
autocalculate`` (this building's floor plates are defined by
``BuildingSurface:Detailed`` polygons, not a literal number), and
``pyenergyplus.exchange`` has no reportable "zone floor area" output
variable. EnergyPlus computes it anyway during input processing and echoes
it, per zone, in the ``.eio`` file's ``Zone Information`` records — including
a "Part of Total Building Area" flag that already excludes unconditioned
zones like the attic. Reading it from there is the only way to get this
number without hard-coding a fact about one specific building into ``src/``
(AGENTS.md invariant #8).

The record's field order is read from its own preceding ``! <Zone
Information>,...`` header comment rather than assumed by position, so a
future EnergyPlus version reordering or adding columns fails loudly (a
missing expected column) instead of silently reading the wrong one.
"""

from __future__ import annotations

from pathlib import Path

from ecoloop.errors import SimulationFatalError
from ecoloop.logging import get_logger

__all__ = ["conditioned_floor_area_m2"]

_logger = get_logger(__name__, component="simulation")

_HEADER_PREFIX = "! <Zone Information>,"
_RECORD_PREFIX = "Zone Information,"
_FLOOR_AREA_COLUMN = "Floor Area {m2}"
_PART_OF_TOTAL_COLUMN = "Part of Total Building Area"


def conditioned_floor_area_m2(path: Path) -> float:
    """Sum the floor area of every zone counted toward total building area.

    Args:
        path: Path to a completed run's ``eplusout.eio``.

    Returns:
        Total conditioned floor area in square metres.

    Raises:
        SimulationFatalError: If the file is missing, or its ``Zone
            Information`` header or records cannot be found/parsed.
    """
    if not path.is_file():
        raise SimulationFatalError("EnergyPlus .eio file not found", path=str(path))

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    header = next((line for line in lines if line.strip().startswith(_HEADER_PREFIX)), None)
    if header is None:
        raise SimulationFatalError(
            "no 'Zone Information' header found in .eio file", path=str(path)
        )
    columns = [field.strip() for field in header.strip().lstrip("!").lstrip().split(",")]
    try:
        floor_area_index = columns.index(_FLOOR_AREA_COLUMN)
        part_of_total_index = columns.index(_PART_OF_TOTAL_COLUMN)
    except ValueError as exc:
        raise SimulationFatalError(
            "'Zone Information' header is missing an expected column",
            path=str(path),
            expected=f"{_FLOOR_AREA_COLUMN!r} and {_PART_OF_TOTAL_COLUMN!r}",
        ) from exc

    total = 0.0
    zones_counted = 0
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(_RECORD_PREFIX):
            continue
        fields = [field.strip() for field in stripped.split(",")]
        if fields[part_of_total_index] != "Yes":
            continue
        total += float(fields[floor_area_index])
        zones_counted += 1

    if zones_counted == 0:
        raise SimulationFatalError(
            "no zone counted toward total building area in .eio file", path=str(path)
        )

    _logger.info(
        "computed conditioned floor area from .eio",
        total_floor_area_m2=total,
        zones_counted=zones_counted,
    )
    return total
