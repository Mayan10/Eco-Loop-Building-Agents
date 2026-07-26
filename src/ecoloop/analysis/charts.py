"""Plotly figure builders for the comparison report.

Every figure is graded from ``analysis.palette`` (config, not a hard-coded
colour list — AGENTS.md invariant #8) so the report and any future dashboard
share one visual language, and so the palette stays colour-blind-safe
(Okabe-Ito derived) by construction rather than by each chart re-picking
colours.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from ecoloop.analysis.compare import ComparisonResult
from ecoloop.config import EcoLoopSettings

__all__ = [
    "comfort_violation_chart",
    "energy_breakdown_chart",
    "energy_total_chart",
    "pmv_timeseries_chart",
]


def _controller_color(settings: EcoLoopSettings, controller: str) -> str:
    """Look up a controller's palette colour, falling back to the violation colour."""
    return getattr(settings.analysis.palette, controller, settings.analysis.palette.violation)


def energy_total_chart(comparison: ComparisonResult, settings: EcoLoopSettings) -> go.Figure:
    """A bar chart of each run's total site energy use, in kWh.

    Args:
        comparison: A fairness-checked comparison across controllers.
        settings: Loaded Eco-Loop settings, for the colour palette.

    Returns:
        A Plotly figure ready to embed or show.
    """
    controllers = [e.manifest.controller for e in comparison.entries]
    totals = [e.energy.total_kwh for e in comparison.entries]
    colors = [_controller_color(settings, c) for c in controllers]
    figure = go.Figure(data=[go.Bar(x=controllers, y=totals, marker_color=colors)])
    figure.update_layout(title="Total site energy use", yaxis_title="kWh", xaxis_title="Controller")
    return figure


def energy_breakdown_chart(comparison: ComparisonResult, settings: EcoLoopSettings) -> go.Figure:
    """A stacked bar chart of per-meter kWh, one stack per controller.

    Args:
        comparison: A fairness-checked comparison across controllers.
        settings: Loaded Eco-Loop settings (unused for colour here - per-meter
            stacks use Plotly's default qualitative palette, since
            ``analysis.palette`` only names per-controller colours).

    Returns:
        A Plotly figure ready to embed or show.
    """
    del settings  # kept for a consistent chart-builder signature
    meter_names = sorted({name for e in comparison.entries for name in e.energy.by_meter_kwh})
    controllers = [e.manifest.controller for e in comparison.entries]
    figure = go.Figure()
    for meter in meter_names:
        figure.add_bar(
            name=meter,
            x=controllers,
            y=[e.energy.by_meter_kwh.get(meter, 0.0) for e in comparison.entries],
        )
    figure.update_layout(
        barmode="stack",
        title="Energy use by meter",
        yaxis_title="kWh",
        xaxis_title="Controller",
    )
    return figure


def comfort_violation_chart(comparison: ComparisonResult, settings: EcoLoopSettings) -> go.Figure:
    """A bar chart of each run's ASHRAE 55 comfort-violation rate.

    Args:
        comparison: A fairness-checked comparison across controllers.
        settings: Loaded Eco-Loop settings, for the colour palette.

    Returns:
        A Plotly figure ready to embed or show.
    """
    controllers = [e.manifest.controller for e in comparison.entries]
    violation_pct = [(e.comfort.violation_fraction or 0.0) * 100.0 for e in comparison.entries]
    colors = [_controller_color(settings, c) for c in controllers]
    figure = go.Figure(data=[go.Bar(x=controllers, y=violation_pct, marker_color=colors)])
    figure.update_layout(
        title="Occupied-zone-timestep comfort violations",
        yaxis_title="% of occupied zone-timesteps outside ASHRAE 55",
        xaxis_title="Controller",
    )
    return figure


def pmv_timeseries_chart(
    telemetry_by_controller: dict[str, pd.DataFrame],
    settings: EcoLoopSettings,
    *,
    zone: str,
) -> go.Figure:
    """Overlay one zone's PMV over time across controllers, with a comfort band.

    Args:
        telemetry_by_controller: Each controller's full telemetry history, as
            read by :func:`ecoloop.analysis.collect.read_telemetry`.
        settings: Loaded Eco-Loop settings, for ``comfort.pmv_occupied_*`` and
            the colour palette.
        zone: Zone name, e.g. ``"CORE_ZN"``.

    Returns:
        A Plotly figure ready to embed or show. Zones without a PMV column
        for a given controller (unconditioned zones, or a run that never
        requested the Fanger output) are silently skipped for that
        controller rather than plotted as zero.
    """
    comfort = settings.comfort
    column = f"zone__{zone.upper()}__pmv"
    figure = go.Figure()
    for controller, df in telemetry_by_controller.items():
        if column not in df.columns or "clock_iso" not in df.columns:
            continue
        clocks = pd.to_datetime(df["clock_iso"])
        figure.add_scatter(
            x=clocks,
            y=df[column],
            mode="lines",
            name=controller,
            line_color=_controller_color(settings, controller),
        )
    figure.add_hrect(
        y0=comfort.pmv_occupied_min,
        y1=comfort.pmv_occupied_max,
        fillcolor=settings.analysis.palette.comfort_band,
        opacity=0.3,
        line_width=0,
        annotation_text="ASHRAE 55 comfort band",
    )
    figure.update_layout(title=f"{zone} PMV over time", yaxis_title="PMV", xaxis_title="Time")
    return figure
