"""Tests for the MCP trace writer."""

from __future__ import annotations

import json
from pathlib import Path

from ecoloop.bus.models import SimClock
from ecoloop.mcp.trace import TraceWriter


class TestRecording:
    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "trace.jsonl"
        TraceWriter(path, max_bytes=1_000_000)
        assert path.parent.is_dir()

    def test_appends_one_json_line_per_call(self, tmp_path: Path) -> None:
        writer = TraceWriter(tmp_path / "trace.jsonl", max_bytes=1_000_000)
        writer.record(tool="get_zone_telemetry", arguments={}, result_summary="ok", sim_clock=None)
        writer.record(
            tool="propose_policy",
            arguments={"zone": "CORE_ZN"},
            result_summary="ok",
            sim_clock=None,
        )
        lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_entry_fields_round_trip(self, tmp_path: Path) -> None:
        writer = TraceWriter(tmp_path / "trace.jsonl", max_bytes=1_000_000)
        clock = SimClock(year=1999, month=6, day=1, hour=12, minute=0, day_of_week=3)
        writer.record(
            tool="propose_policy",
            arguments={"reasoning": "precooling"},
            result_summary="published",
            sim_clock=clock,
            error=None,
        )
        entry = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip())
        assert entry["tool"] == "propose_policy"
        assert entry["arguments"] == {"reasoning": "precooling"}
        assert entry["result_summary"] == "published"
        assert entry["sim_clock"] == clock.isoformat()
        assert entry["error"] is None

    def test_none_sim_clock_is_recorded_as_null(self, tmp_path: Path) -> None:
        writer = TraceWriter(tmp_path / "trace.jsonl", max_bytes=1_000_000)
        writer.record(tool="get_run_manifest", arguments={}, result_summary="ok", sim_clock=None)
        entry = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip())
        assert entry["sim_clock"] is None

    def test_error_field_is_recorded(self, tmp_path: Path) -> None:
        writer = TraceWriter(tmp_path / "trace.jsonl", max_bytes=1_000_000)
        writer.record(
            tool="propose_policy",
            arguments={},
            result_summary="raised",
            sim_clock=None,
            error="boom",
        )
        entry = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip())
        assert entry["error"] == "boom"


class TestSizeCap:
    def test_stops_writing_once_the_cap_is_reached(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.jsonl"
        writer = TraceWriter(path, max_bytes=200)
        for i in range(50):
            writer.record(
                tool="get_zone_telemetry", arguments={"i": i}, result_summary="ok", sim_clock=None
            )
        size_after = path.stat().st_size
        assert size_after <= 200 + 300  # one entry's worth of slack past the cap

        # Confirm writes have actually stopped, not just slowed: recording
        # many more times must not grow the file further.
        for i in range(50, 100):
            writer.record(
                tool="get_zone_telemetry", arguments={"i": i}, result_summary="ok", sim_clock=None
            )
        assert path.stat().st_size == size_after

    def test_cap_warning_flag_flips_once_capacity_is_reached(self, tmp_path: Path) -> None:
        writer = TraceWriter(tmp_path / "trace.jsonl", max_bytes=10)
        assert writer._cap_warning_logged is False
        writer.record(tool="a", arguments={}, result_summary="ok", sim_clock=None)
        # First entry always writes (file didn't exist yet, so _at_capacity()
        # was False going in) - the cap is only checked at the *next* call.
        writer.record(tool="b", arguments={}, result_summary="ok", sim_clock=None)
        assert writer._cap_warning_logged is True
        writer.record(tool="c", arguments={}, result_summary="ok", sim_clock=None)
        assert writer._cap_warning_logged is True  # stays true, not re-toggled
