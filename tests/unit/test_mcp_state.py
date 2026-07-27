"""Tests for standalone MCP server state construction.

``simulation.weather`` normally points at a real EPW file, but that file is
gitignored (copied from the EnergyPlus install per ``AGENTS.md``) and never
present on a clean checkout, including CI's runner. Anything here that needs
weather data points ``simulation.weather`` at a small synthetic EPW built the
same way ``test_weather.py`` does, rather than depending on the real one.
"""

from __future__ import annotations

from pathlib import Path

from test_weather import two_days

from ecoloop.config import load_settings
from ecoloop.mcp.state import create_standalone_state


class TestCreateStandaloneState:
    def test_telemetry_and_policy_start_empty(self) -> None:
        state = create_standalone_state(load_settings(profile="fast"))
        assert state.telemetry.latest() is None
        assert len(state.telemetry) == 0

    def test_zone_map_loads_from_the_real_config(self) -> None:
        state = create_standalone_state(load_settings(profile="fast"))
        assert state.zone_map is not None
        assert state.zone_map.zone("Core_ZN") is not None

    def test_weather_loads_from_the_configured_epw(self, tmp_path: Path) -> None:
        settings = load_settings(
            profile="fast", overrides={"simulation": {"weather": str(two_days(tmp_path))}}
        )
        state = create_standalone_state(settings)
        assert state.weather is not None
        assert len(state.weather.hours) > 0

    def test_trace_writer_is_attached(self) -> None:
        state = create_standalone_state(load_settings(profile="fast"))
        assert state.trace is not None

    def test_sandbox_roots_come_from_settings(self) -> None:
        settings = load_settings(profile="fast")
        state = create_standalone_state(settings)
        assert state.sandbox_roots == settings.mcp.sandbox_roots

    def test_reflex_is_unattached_in_standalone_mode(self) -> None:
        state = create_standalone_state(load_settings(profile="fast"))
        assert state.reflex is None
