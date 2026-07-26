"""Tests for context block assembly and token-budget fitting."""

from __future__ import annotations

from _mcp_state_factory import make_sample, make_state, make_zone

from ecoloop.agent.context import build_context, render_context
from ecoloop.config import ContextSettings


def context_settings(**overrides: object) -> ContextSettings:
    defaults: dict[str, object] = {
        "max_input_tokens": 4000,
        "max_output_tokens": 800,
        "block_priority": (
            "zone_summary",
            "comfort_status",
            "active_policy",
            "energy_demand",
            "alerts",
            "grid_signal",
            "reflection",
            "weather_lookahead",
            "occupancy_forecast",
            "few_shot",
        ),
        "max_tool_result_tokens": 700,
        "chars_per_token": 3.6,
    }
    defaults.update(overrides)
    return ContextSettings(**defaults)  # type: ignore[arg-type]


class TestBuildContext:
    def test_returns_one_block_per_configured_name(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        blocks = build_context(state, context_settings())
        assert {b.name for b in blocks} == set(context_settings().block_priority)

    def test_blocks_are_in_priority_order(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        blocks = build_context(state, context_settings())
        assert [b.name for b in blocks] == list(context_settings().block_priority)

    def test_zone_summary_reports_no_telemetry_when_cold(self) -> None:
        state = make_state()
        blocks = build_context(state, context_settings())
        zone_summary = next(b for b in blocks if b.name == "zone_summary")
        assert "No telemetry" in zone_summary.text

    def test_zone_summary_reflects_a_published_sample(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(
            make_sample(zones=(make_zone("CORE_ZN", air_temperature_c=25.5),))
        )
        blocks = build_context(state, context_settings())
        zone_summary = next(b for b in blocks if b.name == "zone_summary")
        assert "CORE_ZN" in zone_summary.text
        assert "25.5" in zone_summary.text

    def test_comfort_status_names_the_worst_offender(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(
            make_sample(
                zones=(make_zone("CORE_ZN", pmv=0.1), make_zone("PERIMETER_ZN_1", pmv=-1.5))
            )
        )
        blocks = build_context(state, context_settings())
        comfort = next(b for b in blocks if b.name == "comfort_status")
        assert "PERIMETER_ZN_1" in comfort.text


class TestBudgetFitting:
    def test_dropping_from_the_tail_when_over_budget(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        # Tiny budget: only the first block or two should survive.
        tiny_settings = context_settings(max_input_tokens=5)
        blocks = build_context(state, tiny_settings)
        assert len(blocks) < len(tiny_settings.block_priority)
        # Whatever survives must be a prefix of the priority order.
        kept_names = [b.name for b in blocks]
        assert kept_names == list(tiny_settings.block_priority)[: len(kept_names)]

    def test_generous_budget_keeps_every_block(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        blocks = build_context(state, context_settings(max_input_tokens=100_000))
        assert len(blocks) == len(context_settings().block_priority)

    def test_individual_block_is_truncated_past_its_own_cap(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(
            make_sample(zones=tuple(make_zone(f"ZONE_{i}") for i in range(50)))
        )
        tiny_block_cap = context_settings(max_tool_result_tokens=5, max_input_tokens=100_000)
        blocks = build_context(state, tiny_block_cap)
        zone_summary = next(b for b in blocks if b.name == "zone_summary")
        assert "truncated" in zone_summary.text


class TestRenderContext:
    def test_renders_labelled_sections(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        blocks = build_context(state, context_settings())
        rendered = render_context(blocks)
        assert "## zone_summary" in rendered
        assert "## few_shot" in rendered
