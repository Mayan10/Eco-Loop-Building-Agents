"""Tests for parsing conditioned floor area out of a ``.eio`` file.

The synthetic-fixture tests need no EnergyPlus install at all - they exercise
the header-driven column lookup against a hand-built ``.eio`` excerpt. One
energyplus-marked test cross-checks the parser against a real run's output,
confirming the well-known DOE Small Office prototype total (511.16 m2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.config import load_settings
from ecoloop.errors import SimulationFatalError
from ecoloop.runner import run_controller
from ecoloop.simulation.eio import conditioned_floor_area_m2

_SAMPLE_EIO = """\
Program Version,EnergyPlus, Version 25.2.0
! <Zone Information>,Zone Name,North Axis {deg},Origin X-Coordinate {m},Origin Y-Coordinate {m},Origin Z-Coordinate {m},Centroid X-Coordinate {m},Centroid Y-Coordinate {m},Centroid Z-Coordinate {m},Type,Zone Multiplier,Zone List Multiplier,Minimum X {m},Maximum X {m},Minimum Y {m},Maximum Y {m},Minimum Z {m},Maximum Z {m},Ceiling Height {m},Volume {m3},Zone Inside Convection Algorithm {Simple-Detailed-CeilingDiffuser-TrombeWall},Zone Outside Convection Algorithm {Simple-Detailed-Tarp-MoWitt-DOE-2-BLAST}, Floor Area {m2},Exterior Gross Wall Area {m2},Exterior Net Wall Area {m2},Exterior Window Area {m2}, Number of Surfaces, Number of SubSurfaces, Number of Shading SubSurfaces,  Part of Total Building Area
 Zone Information, ATTIC,0.0,0.00,0.00,0.00,13.85,9.23,3.70,1,1,1,-0.60,28.29,-0.60,19.06,3.05,6.33,1.64,720.19,TARP,DOE-2,567.98,0.00,0.00,0.00,13,0,0,No
 Zone Information, CORE_ZN,0.0,0.00,0.00,0.00,13.85,9.23,1.53,1,1,1,5.00,22.69,5.00,13.46,0.00,3.05,3.05,456.46,TARP,DOE-2,149.66,0.00,0.00,0.00,6,0,0,Yes
 Zone Information, PERIMETER_ZN_1,0.0,0.00,0.00,0.00,13.85,2.21,1.53,1,1,1,0.00,27.69,0.00,5.00,0.00,3.05,3.05,346.02,TARP,DOE-2,113.45,84.45,63.82,20.64,6,7,0,Yes
"""


class TestConditionedFloorAreaM2:
    def test_sums_only_zones_counted_toward_total_building_area(self, tmp_path: Path) -> None:
        eio_path = tmp_path / "eplusout.eio"
        eio_path.write_text(_SAMPLE_EIO, encoding="utf-8")

        area = conditioned_floor_area_m2(eio_path)

        assert area == pytest.approx(149.66 + 113.45)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SimulationFatalError, match="not found"):
            conditioned_floor_area_m2(tmp_path / "missing.eio")

    def test_file_with_no_zone_information_header_raises(self, tmp_path: Path) -> None:
        eio_path = tmp_path / "eplusout.eio"
        eio_path.write_text("Program Version,EnergyPlus, Version 25.2.0\n", encoding="utf-8")

        with pytest.raises(SimulationFatalError, match="header"):
            conditioned_floor_area_m2(eio_path)


@pytest.mark.energyplus
class TestAgainstARealRun:
    def test_matches_the_known_doe_small_office_total(self, tmp_path: Path) -> None:
        settings = load_settings(
            profile="fast",
            overrides={"simulation": {"idf_prepared": str(tmp_path / "prepared.idf")}},
        )
        output_dir = tmp_path / "area_run"

        manifest = run_controller(settings, "baseline", profile="fast", output_dir=output_dir)

        area = conditioned_floor_area_m2(manifest.output_dir / "eplusout.eio")
        assert area == pytest.approx(511.16, abs=0.01)
