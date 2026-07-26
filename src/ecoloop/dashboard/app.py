"""Streamlit dashboard: the same comparison report, interactive.

Launched via ``ecoloop dashboard`` (a thin `streamlit run` wrapper - Streamlit
has no supported in-process "run this app" API, only its own CLI entry
point). Deliberately reuses ``analysis.compare``/``analysis.charts`` rather
than recomputing anything: the dashboard and the offline HTML report must
never be able to disagree about a number, since they read the same
manifests through the same functions.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from ecoloop.analysis.charts import (
    comfort_violation_chart,
    energy_breakdown_chart,
    energy_total_chart,
    pmv_timeseries_chart,
)
from ecoloop.analysis.collect import read_telemetry
from ecoloop.analysis.compare import compare_runs, find_latest_runs
from ecoloop.config import load_settings
from ecoloop.errors import EcoLoopError

_REPRESENTATIVE_ZONE = "CORE_ZN"


def main() -> None:
    """Render the dashboard for whatever runs currently exist under results/runs."""
    settings = load_settings()
    st.set_page_config(page_title=settings.analysis.report.title, layout="wide")
    st.title(settings.analysis.report.title)

    discovered = find_latest_runs(settings.resolve(Path("results/runs")))
    if not discovered:
        st.warning(
            "No runs found under results/runs. Run `ecoloop run baseline` and "
            "`ecoloop run rulebased` first."
        )
        return

    order = {"baseline": 0, "rulebased": 1, "agent": 2}
    ranked = sorted(discovered.items(), key=lambda item: order.get(item[0], 99))
    run_dirs = [run_dir for _, run_dir in ranked]

    try:
        comparison = compare_runs(run_dirs, settings)
    except EcoLoopError as exc:
        st.error(f"Cannot compare these runs: {exc}")
        return

    st.subheader("Summary")
    st.dataframe(
        {
            "Controller": [e.manifest.controller for e in comparison.entries],
            "Total kWh": [round(e.energy.total_kwh, 1) for e in comparison.entries],
            "Comfort violation %": [
                round((e.comfort.violation_fraction or 0.0) * 100, 2) for e in comparison.entries
            ],
            "Max |PMV|": [
                round(e.comfort.max_abs_pmv, 2) if e.comfort.max_abs_pmv is not None else None
                for e in comparison.entries
            ],
            "Unmet hours": [round(e.comfort.unmet_hours, 1) for e in comparison.entries],
        },
        hide_index=True,
    )

    left, right = st.columns(2)
    with left:
        st.plotly_chart(energy_total_chart(comparison, settings), use_container_width=True)
        st.plotly_chart(comfort_violation_chart(comparison, settings), use_container_width=True)
    with right:
        st.plotly_chart(energy_breakdown_chart(comparison, settings), use_container_width=True)
        telemetry_by_controller = {
            entry.manifest.controller: read_telemetry(entry.manifest.telemetry_path)
            for entry in comparison.entries
        }
        st.plotly_chart(
            pmv_timeseries_chart(telemetry_by_controller, settings, zone=_REPRESENTATIVE_ZONE),
            use_container_width=True,
        )


main()
