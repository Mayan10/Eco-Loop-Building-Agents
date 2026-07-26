"""Tests for the introspection MCP tools."""

from __future__ import annotations

from pathlib import Path

from _mcp_state_factory import make_sample, make_state, make_zone

from ecoloop.bus.policy import ControlPolicy, PolicySource, ZoneSetpoint
from ecoloop.control.reflex import GuardrailViolationRecord
from ecoloop.mcp import tools_introspect


class TestActivePolicy:
    def test_reports_inactive_with_no_telemetry(self) -> None:
        state = make_state()
        result = tools_introspect.get_active_policy(state)
        assert result.has_active_policy is False

    def test_reports_the_published_policy(self) -> None:
        state = make_state()
        sample = make_sample(zones=(make_zone("CORE_ZN"),))
        state.telemetry.put_nowait(sample)
        state.policy.publish(
            ControlPolicy(
                issued_at=sample.clock,
                source=PolicySource.AGENT,
                ttl_minutes=90.0,
                zone_setpoints=(
                    ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=19.0, cooling_setpoint_c=26.0),
                ),
                reasoning="precooling",
            )
        )
        result = tools_introspect.get_active_policy(state)
        assert result.has_active_policy is True
        assert result.reasoning == "precooling"
        assert result.zone_setpoints["CORE_ZN"] == (19.0, 26.0)


class TestGuardrailViolations:
    def test_empty_without_an_attached_reflex_controller(self) -> None:
        state = make_state()
        assert tools_introspect.get_guardrail_violations(state) == ()

    def test_reports_recorded_violations(self) -> None:
        class FakeReflex:
            def recent_violations(self, count: int) -> tuple[GuardrailViolationRecord, ...]:
                sample = make_sample(zones=(make_zone("CORE_ZN"),))
                return (
                    GuardrailViolationRecord(
                        sim_clock=sample.clock, zone="CORE_ZN", violations=("clamped to envelope",)
                    ),
                )[:count]

        state = make_state(reflex=FakeReflex())
        results = tools_introspect.get_guardrail_violations(state, count=5)
        assert len(results) == 1
        assert results[0].zone == "CORE_ZN"
        assert results[0].violations == ("clamped to envelope",)


class TestRunManifest:
    def test_reports_zero_samples_for_a_fresh_state(self) -> None:
        state = make_state()
        manifest = tools_introspect.get_run_manifest(state)
        assert manifest.published_samples == 0
        assert manifest.dropped_samples == 0
        assert "Core_ZN" in manifest.zones_conditioned

    def test_run_period_info_names_the_active_profile(self) -> None:
        state = make_state()
        info = tools_introspect.get_run_period_info(state)
        assert "profile=fast" in info


class TestRecentErrors:
    def test_empty_without_an_attached_err_file(self) -> None:
        state = make_state()
        assert tools_introspect.get_recent_errors(state) == ()

    def test_filters_by_minimum_severity(self, tmp_path: Path) -> None:
        err_path = tmp_path / "eplusout.err"
        err_path.write_text(
            "   ** Warning ** minor thing\n   ** Severe  ** big problem\n",
            encoding="utf-8",
        )
        state = make_state(err_path=err_path)
        results = tools_introspect.get_recent_errors(state, min_severity="severe")
        assert len(results) == 1
        assert results[0].severity == "severe"

    def test_default_severity_excludes_info(self, tmp_path: Path) -> None:
        err_path = tmp_path / "eplusout.err"
        err_path.write_text("   ** Warning ** something\n", encoding="utf-8")
        state = make_state(err_path=err_path)
        results = tools_introspect.get_recent_errors(state)
        assert len(results) == 1


class TestSandboxedFiles:
    def test_lists_files_under_an_allowlisted_root(self) -> None:
        state = make_state()
        files = tools_introspect.list_sandboxed_files(state, "config/signals")
        assert "carbon_intensity_hourly.csv" in files

    def test_escape_attempt_returns_empty(self) -> None:
        state = make_state()
        assert tools_introspect.list_sandboxed_files(state, "../../../../etc") == ()

    def test_reads_a_file_inside_the_sandbox(self) -> None:
        state = make_state()
        content = tools_introspect.read_sandboxed_text_file(state, "config/signals/tariff_tou.csv")
        assert "price_per_kwh" in content

    def test_escape_attempt_returns_an_error_string_not_file_content(self) -> None:
        state = make_state()
        result = tools_introspect.read_sandboxed_text_file(state, "../../../../etc/passwd")
        assert result.startswith("error:")
