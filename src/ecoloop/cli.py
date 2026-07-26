"""``ecoloop`` command-line interface.

Single Typer entry point for every operation: diagnostics, simulation runs,
comparison, reporting, the dashboard, and the MCP server.

Run ``ecoloop doctor`` first in a fresh checkout — it reports exactly what is
missing and how to fix it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ecoloop import __version__
from ecoloop.agent.selfheal import run_with_self_healing
from ecoloop.config import EcoLoopSettings, load_settings
from ecoloop.doctor import CheckResult, Status, run_checks
from ecoloop.errors import EcoLoopError
from ecoloop.logging import configure_logging
from ecoloop.mcp.server import build_server
from ecoloop.mcp.state import create_standalone_state
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
    exists. A future ``ecoloop run --controller agent`` attaches a live
    simulation's telemetry and policy store to this same server process.
    """
    settings = _load(config, profile)
    configure_logging(settings.logging)
    state = create_standalone_state(settings)
    server = build_server(state)
    chosen = transport or settings.mcp.transport
    server.run(transport=_FASTMCP_TRANSPORT.get(chosen, "stdio"))  # type: ignore[arg-type]


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
