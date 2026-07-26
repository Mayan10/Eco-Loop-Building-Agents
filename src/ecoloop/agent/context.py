"""Assemble the cognitive layer's prompt context, and keep it inside budget.

Ten named blocks, rendered in the priority order ``agent.context.block_priority``
declares, then fit to ``max_input_tokens`` by dropping from the **tail** of
that list first — the lowest-priority blocks are the first to go, never a
mid-block truncation, which would silently hand the model a half-sentence
that reads as a confidently wrong fact rather than an omission.

Token counts here are an *estimate*: ``len(text) / chars_per_token``, not a
real tokenizer. That is a deliberate, documented approximation (config's own
comment calls it "rough"), not a hidden inaccuracy — adding a real tokenizer
dependency for a budgeting heuristic that only needs to be roughly right
would be exactly the kind of premature precision this project's config
philosophy argues against.
"""

from __future__ import annotations

from dataclasses import dataclass

from ecoloop.config import ContextSettings
from ecoloop.mcp import tools_introspect, tools_observe
from ecoloop.mcp.state import ServerState

__all__ = ["ContextBlock", "build_context", "render_context"]

_TRUNCATION_NOTICE = "\n... [truncated to fit the token budget]"

_FEW_SHOT_BLOCK = """\
Two priorities, in order: hold every occupied zone's PMV within ASHRAE 55 \
(-0.5 to +0.5), then minimise energy and carbon subject to that. Comfort \
violations are not a trade-off for savings - a policy that saves energy by \
letting an occupied zone drift outside the band is a failure, not a result.

Example reasoning: a zone is occupied, PMV is +0.1 (comfortable), and grid \
carbon intensity is near its daily trough. There is room to let cooling \
coast slightly warmer without leaving the comfort band, at lower cost and \
lower emissions - propose a small relaxation, not the opposite.

Example reasoning: a zone is occupied, PMV is +0.6 (already outside the \
band, too warm), and the demand status shows the building is approaching \
its peak-shaving cap. Comfort still comes first: tighten that zone's \
cooling setpoint, and look for demand headroom elsewhere (an unoccupied \
zone, dimmable lighting) rather than leaving this zone uncomfortable to \
protect the demand cap.\
"""

_OCCUPANCY_FORECAST_UNAVAILABLE = (
    "Forward-looking occupancy is not available in this build - only each "
    "zone's *current* occupancy_fraction (see zone_summary above) can be "
    "read; there is no occupancy schedule lookahead to query yet."
)


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One named, rendered piece of prompt context."""

    name: str
    text: str


def build_context(state: ServerState, settings: ContextSettings) -> tuple[ContextBlock, ...]:
    """Render every context block and fit them to the input token budget.

    Args:
        state: Server state to read telemetry, policy, and signals from.
        settings: Token budgeting and block-priority settings.

    Returns:
        Blocks in priority order, truncated individually to
        ``max_tool_result_tokens`` and collectively dropped from the tail
        once the running total would exceed ``max_input_tokens``.
    """
    renderers = {
        "zone_summary": _zone_summary_block,
        "comfort_status": _comfort_status_block,
        "active_policy": _active_policy_block,
        "energy_demand": _energy_demand_block,
        "alerts": _alerts_block,
        "grid_signal": _grid_signal_block,
        "reflection": _reflection_block,
        "weather_lookahead": _weather_lookahead_block,
        "occupancy_forecast": _occupancy_forecast_block,
        "few_shot": _few_shot_block,
    }

    per_block_char_cap = int(settings.max_tool_result_tokens * settings.chars_per_token)
    ordered = [
        ContextBlock(name=name, text=_cap(renderers[name](state), per_block_char_cap))
        for name in settings.block_priority
        if name in renderers
    ]
    return _fit_to_budget(ordered, settings)


def render_context(blocks: tuple[ContextBlock, ...]) -> str:
    """Render a sequence of blocks into the final prompt text.

    Args:
        blocks: Blocks to render, in the order they should appear.

    Returns:
        A single string with one labelled section per block.
    """
    return "\n\n".join(f"## {block.name}\n{block.text}" for block in blocks)


def _fit_to_budget(
    blocks: list[ContextBlock], settings: ContextSettings
) -> tuple[ContextBlock, ...]:
    """Keep blocks from the head of the list until the char budget is spent.

    Args:
        blocks: Candidate blocks, highest priority first.
        settings: Token budgeting settings.

    Returns:
        The kept prefix of ``blocks``.
    """
    char_budget = settings.max_input_tokens * settings.chars_per_token
    kept: list[ContextBlock] = []
    used = 0.0
    for block in blocks:
        cost = len(block.text)
        if used + cost > char_budget:
            break
        kept.append(block)
        used += cost
    return tuple(kept)


def _cap(text: str, max_chars: int) -> str:
    """Truncate text to a character cap, with an explicit notice if cut.

    Args:
        text: The text to cap.
        max_chars: Maximum characters to keep.

    Returns:
        ``text`` unchanged if within the cap, otherwise truncated with
        :data:`_TRUNCATION_NOTICE` appended.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE


def _zone_summary_block(state: ServerState) -> str:
    zones = tools_observe.get_zone_telemetry(state)
    if not zones:
        return "No telemetry published yet."
    lines = [
        f"{z.zone}: {z.air_temperature_c:.1f}C, {z.relative_humidity_pct:.0f}% RH, "
        f"heat={z.heating_setpoint_c:.1f}C cool={z.cooling_setpoint_c:.1f}C, "
        f"occupancy={z.occupancy_fraction:.2f}"
        for z in zones
    ]
    return "\n".join(lines)


def _comfort_status_block(state: ServerState) -> str:
    comfort = tools_observe.get_comfort_status(state)
    if not comfort.any_samples_available:
        return "No telemetry published yet."
    if comfort.worst_zone is None:
        return "No zone reports PMV (Fanger model unavailable)."
    return (
        f"Worst comfort offender: {comfort.worst_zone} (|PMV|={comfort.worst_abs_pmv:.2f}). "
        + "; ".join(
            f"{z.zone} PMV={z.pmv:.2f}" if z.pmv is not None else f"{z.zone} PMV=n/a"
            for z in comfort.zones
        )
    )


def _active_policy_block(state: ServerState) -> str:
    policy = tools_introspect.get_active_policy(state)
    if not policy.has_active_policy:
        return "No active policy - the reflex tier is on its fallback rung."
    setpoints = ", ".join(f"{z}={h:.1f}/{c:.1f}C" for z, (h, c) in policy.zone_setpoints.items())
    return (
        f"Source={policy.source}, age={policy.age_minutes:.0f}/{policy.ttl_minutes:.0f} min, "
        f"reasoning: {policy.reasoning or '(none given)'}. Setpoints: {setpoints}"
    )


def _energy_demand_block(state: ServerState) -> str:
    energy = tools_observe.get_energy_totals(state)
    demand = tools_observe.get_demand_status(state)
    return (
        f"Energy over last {energy.window_minutes:.0f} min: {energy.total_kwh:.2f} kWh. "
        f"Rolling demand: {demand.rolling_average_kw:.1f} kW of {demand.demand_cap_kw:.1f} kW cap "
        f"({demand.fraction_of_cap:.0%}){' - APPROACHING CAP' if demand.approaching_cap else ''}."
    )


def _alerts_block(state: ServerState) -> str:
    violations = tools_introspect.get_guardrail_violations(state, count=5)
    errors = tools_introspect.get_recent_errors(state, min_severity="severe", count=5)
    parts = []
    if violations:
        parts.append(
            "Recent guardrail interventions: "
            + "; ".join(f"{v.zone}: {', '.join(v.violations)}" for v in violations)
        )
    if errors:
        parts.append("Recent severe simulation errors: " + "; ".join(e.message for e in errors))
    return " ".join(parts) if parts else "No alerts."


def _grid_signal_block(state: ServerState) -> str:
    carbon = tools_observe.get_carbon_intensity(state)
    tariff = tools_observe.get_tariff(state)
    return (
        f"Current hour carbon intensity: {carbon.value:.0f} gCO2/kWh. "
        f"Current tariff: {tariff.value:.2f} {tariff.unit}."
    )


def _reflection_block(state: ServerState) -> str:
    policy = tools_introspect.get_active_policy(state)
    if not policy.has_active_policy or not policy.reasoning:
        return "No prior agent-sourced decision to reflect on yet."
    return f"Last decision's stated reasoning: {policy.reasoning}"


def _weather_lookahead_block(state: ServerState) -> str:
    forecast = tools_observe.get_weather_forecast(state, hours_ahead=6)
    if not forecast:
        return "No weather forecast available."
    return "; ".join(
        f"+{i}h: {h.dry_bulb_c:.1f}C" if h.dry_bulb_c is not None else f"+{i}h: n/a"
        for i, h in enumerate(forecast)
    )


def _occupancy_forecast_block(state: ServerState) -> str:
    del state
    return _OCCUPANCY_FORECAST_UNAVAILABLE


def _few_shot_block(state: ServerState) -> str:
    del state
    return _FEW_SHOT_BLOCK
