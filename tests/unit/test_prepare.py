"""Tests for IDF preparation.

These need a real EnergyPlus installation (eppy validates every field against
the actual IDD), so the whole module is marked ``energyplus`` and deselected
by CI, which asserts the engine is absent from PATH.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.simulation.idf import (
    has_object_field_value,
    load_idf,
    people_comfort_model,
    set_idd,
)
from ecoloop.simulation.locate import discover_energyplus
from ecoloop.simulation.prepare import has_pmv_output, prepare_idf

pytestmark = pytest.mark.energyplus


@pytest.fixture
def settings(tmp_path: Path) -> EcoLoopSettings:
    """Real settings, with the prepared IDF redirected into tmp_path."""
    return load_settings(
        profile="fast",
        overrides={
            "simulation": {"idf_prepared": str(tmp_path / "prepared.idf")},
        },
    )


class TestPrepareIdf:
    def test_weather_run_period_is_enabled(self, settings: EcoLoopSettings) -> None:
        """Regression test: the baseline IDF ships with weather-file run periods
        disabled (`Run Simulation for Weather File Run Periods = No`), which
        makes EnergyPlus execute only the design-day sizing environments and
        report success having never touched the weather file. Every meter and
        variable resolves fine and reads back as sizing-period noise, so this
        bug produces no exception anywhere - only silently wrong output.
        """
        destination = prepare_idf(settings)
        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        prepared = load_idf(destination)

        control = prepared.idfobjects["SIMULATIONCONTROL"][0]
        assert control.Run_Simulation_for_Weather_File_Run_Periods.upper() == "YES"
        assert control.Run_Simulation_for_Sizing_Periods.upper() == "NO"

    def test_prepared_idf_has_pmv_output(self, settings: EcoLoopSettings) -> None:
        destination = prepare_idf(settings)
        assert destination.is_file()

        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        prepared = load_idf(destination)

        assert has_pmv_output(prepared)
        assert has_object_field_value(
            prepared, "OUTPUT:VARIABLE", "Variable_Name", "Zone Air CO2 Concentration"
        )

    def test_prepared_idf_enables_contaminant_balance(self, settings: EcoLoopSettings) -> None:
        destination = prepare_idf(settings)
        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        prepared = load_idf(destination)

        balance = prepared.idfobjects["ZONEAIRCONTAMINANTBALANCE"]
        assert len(balance) == 1
        assert balance[0].Carbon_Dioxide_Concentration == "Yes"

    def test_all_five_people_objects_declare_fanger(self, settings: EcoLoopSettings) -> None:
        destination = prepare_idf(settings)
        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        prepared = load_idf(destination)

        for people_name in (
            "Core_ZN People",
            "Perimeter_ZN_1 People",
            "Perimeter_ZN_2 People",
            "Perimeter_ZN_3 People",
            "Perimeter_ZN_4 People",
        ):
            assert people_comfort_model(prepared, people_name) == "FANGER"

    def test_running_twice_is_idempotent(self, settings: EcoLoopSettings) -> None:
        """A second prepare pass must not duplicate objects or fail."""
        prepare_idf(settings)
        destination = prepare_idf(settings, idf_path=settings.simulation.idf_prepared)

        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        prepared = load_idf(destination)

        assert len(prepared.idfobjects["ZONEAIRCONTAMINANTBALANCE"]) == 1
        pmv_variables = [
            obj
            for obj in prepared.idfobjects["OUTPUT:VARIABLE"]
            if obj.Variable_Name == "Zone Thermal Comfort Fanger Model PMV"
            and obj.Key_Value == "Core_ZN People"
        ]
        assert len(pmv_variables) == 1
