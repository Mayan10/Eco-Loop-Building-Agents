"""Tests for the self-healing diagnose/repair loop.

`diagnose()` only needs a parsed `ErrFileSummary`, so `TestDiagnose` builds
one directly and runs with no EnergyPlus install. `repair()` and
`run_with_self_healing()` need a real IDD to load/validate/save an IDF, so
those classes are marked `energyplus` and deselected by CI, matching
`test_prepare.py` and `test_energyplus_backend.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.agent.selfheal import diagnose, repair, run_with_self_healing
from ecoloop.config import load_settings
from ecoloop.simulation.errfile import ErrFileSummary, ErrorRecord, Severity
from ecoloop.simulation.idf import load_idf, set_idd
from ecoloop.simulation.locate import discover_energyplus
from ecoloop.simulation.prepare import prepare_idf

_REAL_SEVERE_MESSAGE = (
    "GetZoneAirSetpoints: ThermostatSetpoint:DualSetpoint = CORE_ZN DUALSPSCHED "
    "Heating Setpoint Temperature Schedule Name = HTGSETP_SCH_MISSING, item not found."
)


def _summary_with(*messages: str) -> ErrFileSummary:
    records = tuple(
        ErrorRecord(severity=Severity.SEVERE, message=m, line_number=i, raw=m)
        for i, m in enumerate(messages)
    )
    counts = {Severity.SEVERE: len(records)}
    return ErrFileSummary(
        path=Path("unused.err"),
        records=records,
        counts=counts,
        completed_successfully=False,
        truncated=False,
    )


class TestDiagnose:
    """Regression coverage for the field/object-name split ambiguity.

    EnergyPlus's folded error text has no delimiter between a free-form
    object name and a free-form field label - both are words separated by
    spaces. An open character class for `field` (e.g. `[\\w ]*?Schedule Name`)
    swallows the object name into the field capture instead of stopping at
    the real label; only a closed alternation of known labels avoids it.
    """

    def test_recognises_the_real_energyplus_message(self) -> None:
        summary = _summary_with(_REAL_SEVERE_MESSAGE)

        diagnosis = diagnose(summary)

        assert diagnosis is not None
        assert diagnosis.fault_class == "invalid_schedule_reference"
        assert diagnosis.object_type == "ThermostatSetpoint:DualSetpoint"
        assert diagnosis.field == "Heating Setpoint Temperature Schedule Name"
        assert diagnosis.bad_value == "HTGSETP_SCH_MISSING"

    def test_field_does_not_absorb_the_object_name(self) -> None:
        """The object name itself must never leak into the field capture."""
        summary = _summary_with(_REAL_SEVERE_MESSAGE)

        diagnosis = diagnose(summary)

        assert diagnosis is not None
        assert "CORE_ZN" not in diagnosis.field
        assert "DUALSPSCHED" not in diagnosis.field

    def test_unrecognised_message_yields_no_diagnosis(self) -> None:
        summary = _summary_with("Some other fatal condition entirely unrelated to schedules.")

        assert diagnose(summary) is None

    def test_no_severe_records_yields_no_diagnosis(self) -> None:
        summary = ErrFileSummary(
            path=Path("unused.err"),
            records=(),
            counts={},
            completed_successfully=False,
            truncated=False,
        )

        assert diagnose(summary) is None


@pytest.mark.energyplus
class TestRepairAndRunWithSelfHealing:
    """Needs a real EnergyPlus install: `repair()` validates against the IDD
    via eppy, and `run_with_self_healing()` actually runs the engine to
    reproduce EnergyPlus's exact error text (see AGENTS.md landmines)."""

    @pytest.fixture
    def prepared_broken_idf(self, tmp_path: Path):
        settings = load_settings(
            profile="fast",
            overrides={"simulation": {"idf_prepared": str(tmp_path / "prepared.idf")}},
        )
        destination = prepare_idf(settings, idf_path=Path("models/faults/broken_thermostat.idf"))
        return settings, destination

    def test_repair_fixes_the_invalid_schedule_reference_in_place(
        self, prepared_broken_idf
    ) -> None:
        settings, idf_path = prepared_broken_idf
        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        idf = load_idf(idf_path)
        diagnosis = diagnose(_summary_with(_REAL_SEVERE_MESSAGE))
        assert diagnosis is not None

        assert repair(idf, diagnosis) is True

        thermostat = idf.idfobjects["THERMOSTATSETPOINT:DUALSETPOINT"][0]
        assert thermostat.Heating_Setpoint_Temperature_Schedule_Name == "HTGSETP_SCH"

    def test_run_with_self_healing_recovers_the_broken_fixture(
        self, prepared_broken_idf, tmp_path: Path
    ) -> None:
        settings, idf_path = prepared_broken_idf
        weather_path = settings.resolve(settings.simulation.weather)
        output_dir = tmp_path / "selfheal_run"

        result = run_with_self_healing(
            settings, idf_path=idf_path, weather_path=weather_path, output_dir=output_dir
        )

        assert result.succeeded is True
        assert result.attempts == 2
        assert len(result.diagnoses) == 1
        assert result.diagnoses[0].fault_class == "invalid_schedule_reference"

    def test_run_with_self_healing_gives_up_on_an_unrecognised_fault(self, tmp_path: Path) -> None:
        """A fault outside this module's narrow scope must fail cleanly, not
        loop forever or raise - the retry cap exists precisely for this."""
        settings = load_settings(
            profile="fast",
            overrides={
                "simulation": {"idf_prepared": str(tmp_path / "prepared.idf")},
                "agent": {"selfheal": {"max_retries": 1}},
            },
        )
        install = discover_energyplus(settings.simulation.energyplus_dir)
        set_idd(install.root / "Energy+.idd")
        idf = load_idf(Path("models/baseline/small_office.idf"))
        broken = idf.idfobjects["ZONE"][0]
        broken.Name = ""
        broken_path = tmp_path / "broken_zone_name.idf"
        idf.saveas(str(broken_path))
        weather_path = settings.resolve(settings.simulation.weather)
        output_dir = tmp_path / "selfheal_run_unrecognised"

        result = run_with_self_healing(
            settings, idf_path=broken_path, weather_path=weather_path, output_dir=output_dir
        )

        assert result.succeeded is False
        assert result.attempts >= 1
