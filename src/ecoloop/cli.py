"""``ecoloop`` command-line interface.

Single Typer entry point for every operation: diagnostics, simulation runs,
comparison, reporting, the dashboard, and the MCP server.

Run ``ecoloop doctor`` first in a fresh checkout — it reports exactly what is
missing and how to fix it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ecoloop import __version__
from ecoloop.agent.selfheal import run_with_self_healing
from ecoloop.analysis.compare import compare_runs, find_latest_runs
from ecoloop.analysis.report import build_report
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.doctor import CheckResult, Status, run_checks
from ecoloop.errors import EcoLoopError
from ecoloop.logging import configure_logging
from ecoloop.mcp.server import build_server
from ecoloop.mcp.state import create_standalone_state
from ecoloop.runner import SynchronousController, run_agent_controller, run_controller
from ecoloop.simulation.prepare import prepare_idf

app = typer.Typer(
    name="ecoloop",
    help=(
        "Eco-Loop — an autonomous closed-loop building control agent.\n\n"
        "An EnergyPlus simulation supervised in real time by a local open-source LLM "
        "over MCP. Run 'ecoloop doctor' first."
    ),
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()

# Shared option types, so every command documents them identically.
ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Explicit config file, layered above the profile."),
]
ProfileOption = Annotated[
    str | None,
    typer.Option("--profile", "-p", help="Config profile: fast, full, or demo."),
]

_STATUS_STYLE: dict[Status, tuple[str, str]] = {
    Status.OK: ("[green]✓[/green]", "green"),
    Status.WARN: ("[yellow]![/yellow]", "yellow"),
    Status.FAIL: ("[red]✗[/red]", "red"),
}


def _load(config: Path | None, profile: str | None) -> EcoLoopSettings:
    """Load settings, converting configuration errors into clean CLI exits.

    Args:
        config: Explicit config path, if any.
        profile: Profile name, if any.

    Returns:
        Validated settings.

    Raises:
        typer.Exit: With status 2 when configuration is invalid.
    """
    try:
        return load_settings(config_path=config, profile=profile)
    except EcoLoopError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _version_callback(value: bool) -> None:
    """Print the version and exit when ``--version`` is passed.

    Args:
        value: Whether the flag was supplied.

    Raises:
        typer.Exit: Immediately after printing.
    """
    if value:
        console.print(f"ecoloop {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    _version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Eco-Loop root callback, hosting global options."""


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def _render_results(results: list[CheckResult]) -> None:
    """Render diagnostic results as a table plus remediation panels.

    Args:
        results: Check results in display order.
    """
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("", width=1)
    table.add_column("Check", style="bold")
    table.add_column("Detail", overflow="fold")

    for result in results:
        icon, style = _STATUS_STYLE[result.status]
        table.add_row(icon, Text(result.name, style=style), result.detail)
    console.print(table)

    for result in results:
        if result.status is Status.OK or not result.remediation:
            continue
        _, style = _STATUS_STYLE[result.status]
        label = "required" if result.required else "optional"
        console.print(
            Panel(
                result.remediation,
                title=f"How to fix: {result.name} ({label})",
                border_style=style,
                padding=(0, 1),
            )
        )


@app.command()
def doctor(config: ConfigOption = None, profile: ProfileOption = None) -> None:
    """Diagnose the environment — [bold]run this first[/bold].

    Checks Python, EnergyPlus and its ``pyenergyplus`` package, the LLM
    endpoint and configured models, the building model and weather inputs,
    disk space, and git.

    Exits non-zero only when a *required* check fails, so it works as a CI gate.
    """
    console.print()
    console.print(
        Panel.fit(
            "[bold green]Eco-Loop[/bold green] environment diagnostics",
            border_style="green",
            padding=(0, 2),
        )
    )
    console.print()

    try:
        settings: EcoLoopSettings | None = load_settings(config_path=config, profile=profile)
    except EcoLoopError as exc:
        console.print(f"[yellow]![/yellow] config failed to load: {exc}")
        console.print("  Running environment-only checks.\n")
        settings = None

    configure_logging(settings.logging if settings else None)
    results = run_checks(settings)
    _render_results(results)

    blocking = [r for r in results if r.blocking]
    warnings = [r for r in results if r.status is Status.WARN]

    console.print()
    if blocking:
        names = ", ".join(r.name for r in blocking)
        console.print(f"[red]✗ {len(blocking)} required check(s) failed:[/red] {names}")
        console.print("  The closed loop cannot run until these are resolved.")
        raise typer.Exit(code=1)

    summary = f"[green]✓ Environment is healthy[/green] ({len(results)} checks passed"
    if warnings:
        summary += f", {len(warnings)} optional warning(s)"
    console.print(summary + ")")
    console.print()


# --------------------------------------------------------------------------- #
# mcp serve
# --------------------------------------------------------------------------- #
mcp_app = typer.Typer(help="Serve the building's live state as typed MCP tools.")
app.add_typer(mcp_app, name="mcp")

# Eco-Loop's config uses "stdio"/"http" as the abstract transport choice;
# FastMCP's own run() takes its SDK's literal transport names directly.
_FASTMCP_TRANSPORT: dict[str, str] = {"stdio": "stdio", "http": "streamable-http"}


@mcp_app.command("serve")
def mcp_serve(
    config: ConfigOption = None,
    profile: ProfileOption = None,
    transport: Annotated[
        str | None, typer.Option("--transport", help="stdio or http; defaults to mcp.transport.")
    ] = None,
) -> None:
    """Start the MCP server: the LLM's only window into the building.

    With no simulation attached, every observe/introspect tool reports empty
    or "no data available" results rather than erroring, and every actuate
    tool refuses proposals with a clear reason — this is the standalone mode
    for exploring the tool surface or connecting Claude Desktop before a run
    exists. ``ecoloop run agent`` builds its own separate, in-process server
    bound to that run's live telemetry and policy store — it does not attach
    to a server started this way.
    """
    settings = _load(config, profile)
    configure_logging(settings.logging)
    state = create_standalone_state(settings)
    server = build_server(state)
    chosen = transport or settings.mcp.transport
    server.run(transport=_FASTMCP_TRANSPORT.get(chosen, "stdio"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
_RUN_CONTROLLER_CHOICES = ("baseline", "rulebased", "agent", "all")


@app.command()
def run(
    controller: Annotated[str, typer.Argument(help="baseline, rulebased, agent, or all.")],
    config: ConfigOption = None,
    profile: ProfileOption = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Override the default results/runs/... path."),
    ] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Agent only: Rich terminal dashboard + demo-profile pacing."),
    ] = False,
) -> None:
    """Run a controller against the real EnergyPlus engine end to end.

    ``baseline`` and ``rulebased`` compute their policy synchronously with no
    LLM involved. ``agent`` starts the cognitive tier on its own worker
    thread alongside the live run, calling a real Ollama endpoint — a single
    cognitive cycle has been observed taking on the order of minutes
    end to end (EnergyPlus itself is routinely far faster), so a short
    profile can legitimately complete with few or zero cycles; that is a
    degraded success (AGENTS.md §"Degradation is the normal path"), not a
    failure. ``--live`` (agent only) renders a Rich terminal dashboard and
    paces timesteps by ``agent.live_pacing_seconds_per_timestep``, so the run
    has something to show instead of finishing before the first LLM round
    trip returns — the ``demo`` profile sets that pacing; other profiles
    default it to zero. Every mode persists the full-run telemetry history
    plus a manifest for ``compare``/``report`` to consume. ``all`` runs every
    controller in sequence.
    """
    if controller not in _RUN_CONTROLLER_CHOICES:
        console.print(
            f"[red]Unknown controller {controller!r}.[/red] Choose one of: "
            f"{', '.join(_RUN_CONTROLLER_CHOICES)}."
        )
        raise typer.Exit(code=2)
    if live and controller not in ("agent", "all"):
        console.print("[yellow]![/yellow] --live only affects the agent controller; ignoring it.")
        live = False

    settings = _load(config, profile)
    configure_logging(settings.logging)
    chosen_profile = profile or "fast"

    to_run: list[str] = ["baseline", "rulebased", "agent"] if controller == "all" else [controller]
    exit_code = 0
    for name in to_run:
        console.print(f"Running [bold]{name}[/bold] (profile: {chosen_profile})…")
        try:
            if name == "agent":
                manifest = run_agent_controller(
                    settings, profile=chosen_profile, output_dir=output_dir, live=live
                )
            else:
                manifest = run_controller(
                    settings,
                    cast("SynchronousController", name),
                    profile=chosen_profile,
                    output_dir=output_dir,
                )
        except EcoLoopError as exc:
            console.print(f"[red]✗[/red] {name} failed to start: {exc}")
            exit_code = 1
            continue
        if manifest.succeeded:
            console.print(
                f"[green]✓[/green] {name} succeeded — {manifest.timesteps_published} "
                f"timesteps, telemetry at {manifest.telemetry_path}"
            )
        else:
            console.print(
                f"[red]✗[/red] {name} did not complete successfully — see {manifest.output_dir}"
            )
            exit_code = 1

    if exit_code:
        raise typer.Exit(code=exit_code)


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
def _resolve_run_dirs(
    settings: EcoLoopSettings, latest: bool, runs: list[Path] | None
) -> list[Path]:
    """Resolve --latest/--runs into a concrete, ordered list of run directories."""
    if runs:
        return runs
    discovered = find_latest_runs(settings.resolve(Path("results/runs")))
    if not discovered:
        console.print(
            "[red]No runs found under results/runs.[/red] Run 'ecoloop run baseline' "
            "and 'ecoloop run rulebased' first."
        )
        raise typer.Exit(code=1)
    # A stable, human-meaningful order beats whatever dict-insertion order
    # find_latest_runs happened to produce.
    order = {"baseline": 0, "rulebased": 1, "agent": 2}
    return [
        run_dir
        for _, run_dir in sorted(discovered.items(), key=lambda item: order.get(item[0], 99))
    ]


@app.command()
def compare(
    config: ConfigOption = None,
    profile: ProfileOption = None,
    latest: Annotated[
        bool, typer.Option("--latest", help="Compare each controller's most recent run.")
    ] = True,
    runs: Annotated[
        list[Path] | None,
        typer.Option("--run", help="Explicit run directory; repeatable. Overrides --latest."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Write the comparison as JSON to this path.")
    ] = None,
) -> None:
    """Compare controllers' energy and comfort metrics side by side.

    Refuses (non-zero exit, no partial table) if the runs being compared did
    not share the same profile, weather file, and EnergyPlus version — a
    difference in kWh between runs that faced different conditions measures
    nothing about the controller.
    """
    settings = _load(config, profile)
    configure_logging(settings.logging)
    run_dirs = _resolve_run_dirs(settings, latest, runs)

    try:
        result = compare_runs(run_dirs, settings)
    except EcoLoopError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Controller")
    table.add_column("Total kWh", justify="right")
    table.add_column("Comfort violation %", justify="right")
    table.add_column("Max |PMV|", justify="right")
    table.add_column("Unmet hours", justify="right")
    for entry in result.entries:
        violation_pct = (
            f"{entry.comfort.violation_fraction * 100:.2f}%"
            if entry.comfort.violation_fraction is not None
            else "n/a"
        )
        max_pmv = (
            f"{entry.comfort.max_abs_pmv:.2f}" if entry.comfort.max_abs_pmv is not None else "n/a"
        )
        table.add_row(
            entry.manifest.controller,
            f"{entry.energy.total_kwh:.1f}",
            violation_pct,
            max_pmv,
            f"{entry.comfort.unmet_hours:.1f}",
        )
    console.print(table)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        console.print(f"Wrote comparison to {output}")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
@app.command()
def report(
    config: ConfigOption = None,
    profile: ProfileOption = None,
    latest: Annotated[
        bool, typer.Option("--latest", help="Report on each controller's most recent run.")
    ] = True,
    runs: Annotated[
        list[Path] | None,
        typer.Option("--run", help="Explicit run directory; repeatable. Overrides --latest."),
    ] = None,
    output: Annotated[Path, typer.Option("--output", help="Destination HTML file.")] = Path(
        "results/report.html"
    ),
) -> None:
    """Build a single, self-contained offline HTML comparison report.

    Opens with no server and no internet connection: Plotly's JS is embedded
    directly in the file when ``analysis.report.embed_plotly_js`` is set
    (the default). Refuses, like `compare`, if the runs being reported on
    don't share the same profile, weather file, and EnergyPlus version.
    """
    settings = _load(config, profile)
    configure_logging(settings.logging)
    run_dirs = _resolve_run_dirs(settings, latest, runs)

    try:
        comparison = compare_runs(run_dirs, settings)
    except EcoLoopError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    destination = build_report(comparison, settings, output)
    console.print(f"[green]✓[/green] Wrote report to {destination}")


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
_DASHBOARD_APP_PATH = Path(__file__).parent / "dashboard" / "app.py"


@app.command()
def dashboard() -> None:
    """Launch the interactive Streamlit dashboard.

    ``streamlit`` is an optional dependency (``pip install -e ".[dashboard]"``)
    since the offline HTML report (``ecoloop report``) covers the same
    numbers without it. Streamlit has no supported in-process "run this app"
    API, so this shells out to its own CLI entry point with a fixed,
    hard-coded argument list — no user or LLM-controlled input ever reaches
    this command.
    """
    streamlit_bin = shutil.which("streamlit")
    if streamlit_bin is None:
        console.print(
            "[red]streamlit is not installed.[/red] Run: "
            '[bold]uv pip install -e ".[dashboard]"[/bold]'
        )
        raise typer.Exit(code=1)
    result = subprocess.run(  # noqa: S603
        [streamlit_bin, "run", str(_DASHBOARD_APP_PATH)], check=False
    )
    raise typer.Exit(code=result.returncode)


# --------------------------------------------------------------------------- #
# selfheal
# --------------------------------------------------------------------------- #
@app.command()
def selfheal(
    idf: Annotated[
        Path, typer.Option("--idf", help="A (possibly deliberately broken) IDF to run.")
    ],
    config: ConfigOption = None,
    profile: ProfileOption = None,
) -> None:
    """Run an IDF, diagnosing and patching a recognised fatal fault on failure.

    Deliberately narrow: only the fault class the ``models/faults/`` demo
    fixtures inject (an invalid schedule-name reference) is recognised — see
    ``agent/selfheal.py`` for why that scope is honest rather than limiting.
    Bounded by ``agent.selfheal.max_retries``; never loops forever.
    """
    settings = _load(config, profile)
    configure_logging(settings.logging)

    console.print(f"Preparing [bold]{idf}[/bold] (weather run periods, comfort instrumentation)…")
    prepared = prepare_idf(settings, idf_path=idf)
    weather_path = settings.resolve(settings.simulation.weather)
    output_dir = settings.project.results_dir / "selfheal" / idf.stem

    result = run_with_self_healing(
        settings, idf_path=prepared, weather_path=weather_path, output_dir=output_dir
    )

    for i, diagnosis in enumerate(result.diagnoses, start=1):
        console.print(
            f"  attempt {i}: diagnosed [yellow]{diagnosis.fault_class}[/yellow] — "
            f"{diagnosis.object_type} / {diagnosis.field} = {diagnosis.bad_value!r}"
        )

    if result.succeeded:
        console.print(f"[green]✓[/green] {result.message}")
        console.print(f"  final IDF: {result.final_idf_path}")
    else:
        console.print(f"[red]✗[/red] {result.message}")
        raise typer.Exit(code=1)


def main() -> None:
    """Console-script entry point declared in ``pyproject.toml``."""
    app()


if __name__ == "__main__":
    main()
