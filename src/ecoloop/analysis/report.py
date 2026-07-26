"""Assemble a single, self-contained offline HTML comparison report.

"Offline" is the point: the report must open with no server and no internet
connection, since ``analysis.report.embed_plotly_js`` controls whether the
~4 MB Plotly library is embedded directly in the file rather than fetched
from a CDN. Only the first figure embeds it; every subsequent figure on the
page reuses the same in-page copy.
"""

from __future__ import annotations

import html
from pathlib import Path

import plotly.graph_objects as go

from ecoloop.analysis.charts import (
    comfort_violation_chart,
    energy_breakdown_chart,
    energy_total_chart,
    pmv_timeseries_chart,
)
from ecoloop.analysis.collect import read_telemetry
from ecoloop.analysis.compare import ComparisonResult
from ecoloop.config import EcoLoopSettings

__all__ = ["build_report"]

_REPRESENTATIVE_ZONE = "CORE_ZN"


def build_report(
    comparison: ComparisonResult, settings: EcoLoopSettings, output_path: Path
) -> Path:
    """Render a comparison to a single self-contained HTML file.

    Args:
        comparison: A fairness-checked comparison across controllers.
        settings: Loaded Eco-Loop settings, for the report title and whether
            to embed Plotly's JS inline.
        output_path: Destination ``.html`` file. Parent directories are
            created if needed.

    Returns:
        ``output_path``, for chaining.
    """
    telemetry_by_controller = {
        entry.manifest.controller: read_telemetry(entry.manifest.telemetry_path)
        for entry in comparison.entries
    }

    figures: list[go.Figure] = [
        energy_total_chart(comparison, settings),
        energy_breakdown_chart(comparison, settings),
        comfort_violation_chart(comparison, settings),
        pmv_timeseries_chart(telemetry_by_controller, settings, zone=_REPRESENTATIVE_ZONE),
    ]

    sections = [_summary_table_html(comparison)]
    for i, figure in enumerate(figures):
        include_js = settings.analysis.report.embed_plotly_js and i == 0
        sections.append(
            figure.to_html(
                full_html=False,
                include_plotlyjs="inline" if include_js else False,
                config={"displaylogo": False},
            )
        )

    title = html.escape(settings.analysis.report.title)
    page = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{''.join(sections)}</body></html>"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(page, encoding="utf-8")
    return output_path


def _summary_table_html(comparison: ComparisonResult) -> str:
    """A plain HTML summary table, matching the CLI's `compare` output."""
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(entry.manifest.controller)}</td>"
        f"<td>{entry.energy.total_kwh:.1f}</td>"
        f"<td>{_percent(entry.comfort.violation_fraction)}</td>"
        f"<td>{_number(entry.comfort.max_abs_pmv)}</td>"
        f"<td>{entry.comfort.unmet_hours:.1f}</td>"
        "</tr>"
        for entry in comparison.entries
    )
    return (
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>Controller</th><th>Total kWh</th><th>Comfort violation %</th>"
        "<th>Max |PMV|</th><th>Unmet hours</th></tr>" + rows + "</table>"
    )


def _percent(value: float | None) -> str:
    return f"{value * 100:.2f}%" if value is not None else "n/a"


def _number(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"
