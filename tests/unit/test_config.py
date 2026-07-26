"""Tests for the layered configuration system."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.config import EcoLoopSettings, _deep_merge, _interpolate, load_settings
from ecoloop.errors import ConfigError


class TestLoading:
    def test_defaults_load_and_validate(self) -> None:
        settings = load_settings()
        assert settings.simulation.timesteps_per_hour > 0
        assert settings.analysis.joules_per_kwh == 3_600_000.0

    @pytest.mark.parametrize("profile", ["fast", "full", "demo"])
    def test_every_shipped_profile_loads(self, profile: str) -> None:
        settings = load_settings(profile=profile)
        assert settings.profile == profile

    def test_profile_overlays_default(self) -> None:
        default = load_settings()
        fast = load_settings(profile="fast")
        # fast.yaml narrows the run period to two weeks.
        assert default.simulation.run_period.is_annual
        assert not fast.simulation.run_period.is_annual

    def test_profile_overlay_is_partial(self) -> None:
        """A profile overriding one field must not erase its siblings."""
        fast = load_settings(profile="fast")
        # fast.yaml sets agent.cadence_minutes but says nothing about triggers.
        assert fast.agent.cadence_minutes == 30
        assert fast.agent.triggers.pmv_excursion is True

    def test_unknown_profile_lists_available(self) -> None:
        with pytest.raises(ConfigError, match="unknown profile"):
            load_settings(profile="does-not-exist")

    def test_missing_config_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_settings(config_path=tmp_path / "nope.yaml")

    def test_settings_are_frozen(self) -> None:
        settings = load_settings()
        with pytest.raises(Exception, match=r"frozen|immutable"):
            settings.simulation.timesteps_per_hour = 12  # type: ignore[misc]

    def test_source_paths_recorded_for_provenance(self) -> None:
        settings = load_settings(profile="fast")
        names = [p.name for p in settings.source_paths]
        assert names == ["default.yaml", "fast.yaml"]


class TestValidation:
    def test_timestep_must_divide_an_hour(self) -> None:
        with pytest.raises(ConfigError, match="divide 60"):
            load_settings(overrides={"simulation": {"timesteps_per_hour": 7}})

    def test_setpoint_envelope_must_be_ordered(self) -> None:
        with pytest.raises(ConfigError, match="heating_setpoint_min_c"):
            load_settings(overrides={"guardrails": {"heating_setpoint_min_c": 99.0}})

    def test_envelope_narrower_than_deadband_is_rejected(self) -> None:
        """An envelope that cannot satisfy the deadband makes clamping unsolvable."""
        with pytest.raises(ConfigError, match="narrower than"):
            load_settings(
                overrides={
                    "guardrails": {
                        "heating_setpoint_min_c": 22.0,
                        "heating_setpoint_max_c": 22.5,
                        "cooling_setpoint_min_c": 22.6,
                        "cooling_setpoint_max_c": 23.0,
                        "min_deadband_c": 5.0,
                    }
                }
            )

    def test_comfort_band_must_be_non_degenerate(self) -> None:
        with pytest.raises(ConfigError, match="pmv_occupied_min"):
            load_settings(overrides={"comfort": {"pmv_occupied_min": 1.0}})

    def test_policy_max_age_cannot_precede_ttl(self) -> None:
        with pytest.raises(ConfigError, match="max_age_minutes"):
            load_settings(
                overrides={"bus": {"policy": {"default_ttl_minutes": 90, "max_age_minutes": 10}}}
            )

    def test_unknown_key_is_rejected_not_ignored(self) -> None:
        """A typo in default.yaml must fail loudly, not be silently dropped."""
        with pytest.raises(ConfigError):
            load_settings(overrides={"simulation": {"timesteps_per_hourr": 6}})


class TestInterpolation:
    def test_expands_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ECOLOOP_TEST_VAR", "hello")
        assert _interpolate("${ECOLOOP_TEST_VAR}") == "hello"

    def test_uses_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ECOLOOP_ABSENT", raising=False)
        assert _interpolate("${ECOLOOP_ABSENT:-fallback}") == "fallback"

    def test_unset_without_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ECOLOOP_ABSENT", raising=False)
        with pytest.raises(ConfigError, match="unset and has no default"):
            _interpolate("${ECOLOOP_ABSENT}")

    def test_recurses_through_containers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ECOLOOP_TEST_VAR", "x")
        assert _interpolate({"a": ["${ECOLOOP_TEST_VAR}", 1]}) == {"a": ["x", 1]}

    def test_leaves_non_strings_alone(self) -> None:
        assert _interpolate(42) == 42
        assert _interpolate(None) is None


class TestDeepMerge:
    def test_nested_mappings_merge_key_by_key(self) -> None:
        merged = _deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}})
        assert merged == {"a": {"x": 1, "y": 3}}

    def test_non_mappings_are_replaced_wholesale(self) -> None:
        assert _deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}

    def test_inputs_are_not_mutated(self) -> None:
        base = {"a": {"x": 1}}
        _deep_merge(base, {"a": {"x": 2}})
        assert base == {"a": {"x": 1}}


class TestPathResolution:
    def test_relative_paths_resolve_against_project_root(self, settings: EcoLoopSettings) -> None:
        resolved = settings.resolve(Path("models/baseline/small_office.idf"))
        assert resolved.is_absolute()
        assert resolved.parts[-3:] == ("models", "baseline", "small_office.idf")

    def test_absolute_paths_pass_through(self, settings: EcoLoopSettings) -> None:
        absolute = Path("/opt/elsewhere.idf")
        assert settings.resolve(absolute) == absolute


class TestSecrets:
    def test_api_key_reads_from_environment_not_config(
        self, settings: EcoLoopSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(settings.llm.api_key_env, "secret-value")
        assert settings.llm.api_key == "secret-value"

    def test_absent_api_key_is_none(
        self, settings: EcoLoopSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(settings.llm.api_key_env, raising=False)
        assert settings.llm.api_key is None
