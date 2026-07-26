"""Tests for standalone MCP server state construction."""

from __future__ import annotations

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

    def test_weather_loads_from_the_configured_epw(self) -> None:
        state = create_standalone_state(load_settings(profile="fast"))
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
