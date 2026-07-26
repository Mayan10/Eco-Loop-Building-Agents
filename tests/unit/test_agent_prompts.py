"""Tests for system prompt rendering."""

from __future__ import annotations

from ecoloop.agent.prompts import CURRENT_PROMPT_VERSION, render_system_prompt


class TestRenderSystemPrompt:
    def test_embeds_the_context_verbatim(self) -> None:
        text, _version = render_system_prompt(
            context="## zone_summary\nCore_ZN: 24.0C", zone_names=["Core_ZN"]
        )
        assert "## zone_summary\nCore_ZN: 24.0C" in text

    def test_lists_the_addressable_zones(self) -> None:
        text, _version = render_system_prompt(context="", zone_names=["Core_ZN", "Perimeter_ZN_1"])
        assert "Core_ZN, Perimeter_ZN_1" in text

    def test_names_the_actuate_tools(self) -> None:
        text, _version = render_system_prompt(context="", zone_names=[])
        assert "propose_policy" in text
        assert "request_zone_setpoint" in text

    def test_returns_the_current_version(self) -> None:
        _text, version = render_system_prompt(context="", zone_names=[])
        assert version == CURRENT_PROMPT_VERSION

    def test_no_zones_renders_without_raising(self) -> None:
        text, _version = render_system_prompt(context="", zone_names=[])
        assert isinstance(text, str)
