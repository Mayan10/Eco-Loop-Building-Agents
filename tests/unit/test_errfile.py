"""Tests for EnergyPlus .err parsing.

These build small synthetic .err fixtures rather than depending on a real
EnergyPlus run, so they pass with the engine absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.errors import SimulationFatalError
from ecoloop.simulation.errfile import Severity, parse_err_file

_MAX_BYTES = 1_000_000


def write_err(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "eplusout.err"
    path.write_text(content, encoding="utf-8")
    return path


class TestBasicParsing:
    def test_parses_severe_and_warning(self, tmp_path: Path) -> None:
        content = (
            "Program Version,EnergyPlus, Version 25.2.0\n"
            "   ** Warning ** Weather file location differs from IDF\n"
            "   ** Severe  ** Zone unoccupied for all hours\n"
            "   **  Fatal  ** Cannot continue\n"
            "EnergyPlus Terminated--Error(s) Detected\n"
        )
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert summary.counts[Severity.WARNING] == 1
        assert summary.counts[Severity.SEVERE] == 1
        assert summary.counts[Severity.FATAL] == 1
        assert not summary.completed_successfully

    def test_success_trailer_is_detected(self, tmp_path: Path) -> None:
        content = (
            "   ** Warning ** minor issue\n"
            "EnergyPlus Completed Successfully-- 1 Warning; 0 Severe Errors\n"
        )
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert summary.completed_successfully

    def test_empty_file_has_no_records(self, tmp_path: Path) -> None:
        summary = parse_err_file(write_err(tmp_path, ""), max_bytes=_MAX_BYTES)
        assert summary.records == ()
        assert summary.worst_severity is None


class TestContinuationLines:
    def test_continuation_folds_into_previous_record(self, tmp_path: Path) -> None:
        content = (
            "   ** Severe  ** Surface does not intersect adjacent zone\n"
            "   **   ~~~   ** Check the geometry near Perimeter_ZN_1\n"
            "   **   ~~~   ** and re-run.\n"
        )
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert summary.counts[Severity.SEVERE] == 1
        assert len(summary.records) == 1
        message = summary.records[0].message
        assert "geometry near Perimeter_ZN_1" in message
        assert "re-run" in message

    def test_continuation_before_any_record_is_ignored(self, tmp_path: Path) -> None:
        content = "   **   ~~~   ** orphan continuation\n   ** Warning ** real one\n"
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert len(summary.records) == 1
        assert summary.records[0].message == "real one"


class TestSeverityFilteringAndOrdering:
    def test_worst_severity_is_fatal_when_present(self, tmp_path: Path) -> None:
        content = "   ** Warning ** a\n   ** Severe  ** b\n   **  Fatal  ** c\n"
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert summary.worst_severity == Severity.FATAL

    def test_filter_returns_only_matching_severity(self, tmp_path: Path) -> None:
        content = "   ** Warning ** a\n   ** Warning ** b\n   ** Severe  ** c\n"
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert len(summary.filter(Severity.WARNING)) == 2
        assert len(summary.filter(Severity.SEVERE)) == 1


class TestUntrustedInputHandling:
    def test_control_characters_are_stripped(self, tmp_path: Path) -> None:
        content = "   ** Warning ** bad\x07bell and \x1bescape\n"
        summary = parse_err_file(write_err(tmp_path, content), max_bytes=_MAX_BYTES)
        assert "\x07" not in summary.records[0].message
        assert "\x1b" not in summary.records[0].message

    def test_oversized_file_is_truncated_but_keeps_head_and_tail(self, tmp_path: Path) -> None:
        head = "   ** Warning ** first issue\n"
        padding = "   ** Warning ** filler\n" * 10_000
        tail = "EnergyPlus Completed Successfully-- 1 Warning; 0 Severe Errors\n"
        path = write_err(tmp_path, head + padding + tail)
        summary = parse_err_file(path, max_bytes=2048)
        assert summary.truncated
        assert summary.completed_successfully
        assert any("first issue" in r.message for r in summary.records)


class TestErrors:
    def test_missing_file_raises_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(SimulationFatalError, match="not found"):
            parse_err_file(tmp_path / "does-not-exist.err", max_bytes=_MAX_BYTES)
