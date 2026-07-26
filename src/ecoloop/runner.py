"""Drive a full EnergyPlus run with a controller attached, end to end.

This is the piece AGENTS.md's landmines call "the CLI's `run` subcommand —
does not exist yet": everything it wires together (a real
:class:`~ecoloop.simulation.energyplus.EnergyPlusBackend`, a resolved
:class:`~ecoloop.simulation.handles.HandleRegistry`,
:class:`~ecoloop.simulation.callbacks.ReflexCallbacks`, and a
:class:`~ecoloop.control.reflex.ReflexController`) already existed and was
already proven correct — against the real engine, for ``baseline`` and
``rulebased``, and against the fake backend for all three modes — but only
inside test helpers. This module is that same wiring, promoted to production
code, plus persistence of the run's telemetry history via
:mod:`ecoloop.analysis.collect` so a comparison report has something to read
afterward.

**``agent`` gets the same treatment, plus a worker thread.** It reads from a
:class:`~ecoloop.bus.policy.PolicyStore` that only a worker thread running the
cognitive tier's cadence loop can keep current — :func:`run_agent_controller`
starts a :class:`~ecoloop.agent.worker.CognitiveWorker` on its own thread
before EnergyPlus's blocking, main-thread-owning ``run()`` call, and stops it
once that call returns. The two threads share exactly the two objects the
architecture allows to cross the boundary — a live
:class:`~ecoloop.bus.telemetry.TelemetryBus` and
:class:`~ecoloop.bus.policy.PolicyStore` — and nothing else.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from ecoloop.agent.llm import LLMClient, OllamaClient
from ecoloop.agent.worker import CognitiveWorker
from ecoloop.analysis.collect import TelemetryRecorder
from ecoloop.bus.models import TelemetrySample
from ecoloop.bus.policy import PolicyStore
from ecoloop.bus.telemetry import TelemetryBus
from ecoloop.config import EcoLoopSettings
from ecoloop.control.reflex import ReflexController
from ecoloop.errors import EcoLoopError
from ecoloop.logging import get_logger
from ecoloop.mcp.server import build_server
from ecoloop.mcp.state import ServerState
from ecoloop.mcp.trace import TraceWriter
from ecoloop.simulation.callbacks import ReflexCallbacks
from ecoloop.simulation.eio import conditioned_floor_area_m2
from ecoloop.simulation.energyplus import EnergyPlusBackend
from ecoloop.simulation.errfile import parse_err_file
from ecoloop.simulation.handles import HandleRegistry, load_zone_map
from ecoloop.simulation.idf import set_idd
from ecoloop.simulation.locate import EnergyPlusInstall, discover_energyplus
from ecoloop.simulation.prepare import prepare_idf
from ecoloop.simulation.weather import load_epw
from ecoloop.ui.live import LiveDashboard

__all__ = [
    "RunManifest",
    "SynchronousController",
    "run_agent_controller",
    "run_controller",
]

_logger = get_logger(__name__, component="runner")

SynchronousController = Literal["baseline", "rulebased"]
_SYNCHRONOUS_CONTROLLERS: tuple[str, ...] = get_args(SynchronousController)


class RunManifest(BaseModel):
    """A record of one completed (or failed) run, for `compare`/`report` to consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    controller: str
    profile: str
    energyplus_version: str
    started_at: datetime
    ended_at: datetime
    idf_path: Path
    weather_path: Path
    output_dir: Path
    telemetry_path: Path
    timesteps_published: int
    dropped_samples: int
    """Always ``0`` for `run_controller`: it records via `TelemetryRecorder` directly, not
    through a `TelemetryBus`, since baseline/rulebased have no worker thread to need one for.
    `run_agent_controller` does run a live `TelemetryBus` (for the cognitive worker thread to
    read) and reports its real drop count here."""
    exit_code: int
    succeeded: bool
    conditioned_floor_area_m2: float | None
    """``None`` when the run failed before an ``.eio`` file could be parsed."""

    def write(self, path: Path) -> None:
        """Write this manifest to ``path`` as JSON.

        Args:
            path: Destination file. Parent directories are created if needed.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")


def run_controller(
    settings: EcoLoopSettings,
    controller: SynchronousController,
    *,
    profile: str = "fast",
    idf_path: Path | None = None,
    weather_path: Path | None = None,
    output_dir: Path | None = None,
) -> RunManifest:
    """Run one synchronous controller against the real EnergyPlus engine.

    Args:
        settings: Loaded Eco-Loop settings, with ``control.controller`` set
            (or overridden) to match ``controller``.
        controller: Which synchronous controller to run — ``"baseline"`` or
            ``"rulebased"``. The ``agent`` controller is out of scope here;
            see the module docstring.
        profile: Label recorded in the manifest and used to build a default
            ``output_dir`` — does not itself select a profile's IDF run
            period; that is `load_settings`'s job, before this is called.
        idf_path: Override for the source IDF; defaults to
            ``simulation.idf_baseline``.
        weather_path: Override for the weather file; defaults to
            ``simulation.weather``.
        output_dir: Override for the run's output directory; defaults to
            ``results/runs/<profile>/<controller>/<timestamp>/``.

    Returns:
        A manifest describing the completed run: whether it succeeded, how
        many timesteps were published, and where its telemetry history and
        EnergyPlus outputs live.

    Raises:
        ValueError: If ``controller`` is not a synchronous controller.
    """
    if controller not in _SYNCHRONOUS_CONTROLLERS:
        raise ValueError(
            f"run_controller only supports {_SYNCHRONOUS_CONTROLLERS!r}; got {controller!r} "
            "- the agent controller needs the cognitive worker-thread wiring, a distinct "
            "integration task (see agent/AGENTS.md)"
        )
    if settings.control.controller != controller:
        settings = settings.model_copy(
            update={"control": settings.control.model_copy(update={"controller": controller})}
        )

    install = discover_energyplus(settings.simulation.energyplus_dir)
    set_idd(install.root / "Energy+.idd")

    prepared_idf = prepare_idf(settings, idf_path=idf_path)
    resolved_weather = weather_path or settings.resolve(settings.simulation.weather)
    resolved_output_dir = output_dir or (
        settings.project.results_dir / "runs" / profile / controller / _timestamp_label()
    )

    zone_map = load_zone_map(settings.resolve(Path("config/zones.yaml")))
    conditioned_names = tuple(zone.name for zone in zone_map.zones if zone.conditioned)

    reflex = ReflexController(
        zone_names=conditioned_names,
        control=settings.control,
        guardrails=settings.guardrails,
        occupied_threshold_fraction=settings.comfort.occupied_threshold_fraction,
        ttl_minutes=settings.bus.policy.default_ttl_minutes,
    )
    recorder = TelemetryRecorder()
    registry = HandleRegistry(zone_map)
    backend = EnergyPlusBackend(install)
    callbacks = ReflexCallbacks(
        backend, zone_map, registry, recorder.record, reflex_controller=reflex
    )

    started_at = datetime.now(UTC)
    registry.request_all(backend)
    callbacks.register()
    _logger.info(
        "starting run",
        controller=controller,
        profile=profile,
        idf=str(prepared_idf),
        output_dir=str(resolved_output_dir),
    )
    exit_code = backend.run(
        idf_path=prepared_idf, weather_path=resolved_weather, output_dir=resolved_output_dir
    )
    ended_at = datetime.now(UTC)

    return _finalize_run(
        controller=controller,
        profile=profile,
        settings=settings,
        install=install,
        started_at=started_at,
        ended_at=ended_at,
        prepared_idf=prepared_idf,
        resolved_weather=resolved_weather,
        resolved_output_dir=resolved_output_dir,
        recorder=recorder,
        exit_code=exit_code,
        dropped_samples=0,
    )


def run_agent_controller(
    settings: EcoLoopSettings,
    *,
    profile: str = "fast",
    idf_path: Path | None = None,
    weather_path: Path | None = None,
    output_dir: Path | None = None,
    llm: LLMClient | None = None,
    live: bool = False,
) -> RunManifest:
    """Run the agent controller, with the cognitive tier on its own worker thread.

    Args:
        settings: Loaded Eco-Loop settings; ``control.controller`` is forced
            to ``"agent"`` if not already set.
        profile: Label recorded in the manifest and used to build a default
            ``output_dir``.
        idf_path: Override for the source IDF; defaults to
            ``simulation.idf_baseline``.
        weather_path: Override for the weather file; defaults to
            ``simulation.weather``.
        output_dir: Override for the run's output directory; defaults to
            ``results/runs/<profile>/agent/<timestamp>/``.
        llm: Override for the LLM client — a real
            :class:`~ecoloop.agent.llm.OllamaClient` against ``settings.llm``
            if omitted; tests pass a scripted double.
        live: Render a Rich terminal dashboard and pace timesteps by
            ``agent.live_pacing_seconds_per_timestep`` (zero by default, so
            passing ``live=True`` without a profile that sets it non-zero
            renders a dashboard with nothing new to show between frames).

    Returns:
        A manifest describing the completed run, in the same shape
        :func:`run_controller` returns.
    """
    if settings.control.controller != "agent":
        settings = settings.model_copy(
            update={"control": settings.control.model_copy(update={"controller": "agent"})}
        )

    install = discover_energyplus(settings.simulation.energyplus_dir)
    set_idd(install.root / "Energy+.idd")

    prepared_idf = prepare_idf(settings, idf_path=idf_path)
    resolved_weather = weather_path or settings.resolve(settings.simulation.weather)
    resolved_output_dir = output_dir or (
        settings.project.results_dir / "runs" / profile / "agent" / _timestamp_label()
    )
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    zone_map = load_zone_map(settings.resolve(Path("config/zones.yaml")))
    conditioned_names = tuple(zone.name for zone in zone_map.zones if zone.conditioned)

    telemetry_bus = TelemetryBus(capacity=settings.bus.telemetry_capacity)
    policy_store = PolicyStore(
        default_ttl_minutes=settings.bus.policy.default_ttl_minutes,
        max_age_minutes=settings.bus.policy.max_age_minutes,
    )
    reflex = ReflexController(
        zone_names=conditioned_names,
        control=settings.control,
        guardrails=settings.guardrails,
        occupied_threshold_fraction=settings.comfort.occupied_threshold_fraction,
        ttl_minutes=settings.bus.policy.default_ttl_minutes,
        policy_store=policy_store,
    )

    state = ServerState(
        settings=settings,
        telemetry=telemetry_bus,
        policy=policy_store,
        zone_map=zone_map,
        reflex=reflex,
        weather=load_epw(resolved_weather),
        err_path=resolved_output_dir / "eplusout.err",
        trace=TraceWriter(
            resolved_output_dir / "agent_trace.jsonl",
            max_bytes=settings.logging.max_trace_mib * 1024 * 1024,
        ),
        sandbox_roots=settings.mcp.sandbox_roots,
    )
    server = build_server(state)
    llm_client = llm or OllamaClient(
        settings.llm, cache_dir=settings.resolve(settings.llm.cache.dir)
    )
    worker = CognitiveWorker(state, server, llm_client, settings.agent)
    dashboard = LiveDashboard(state, worker, title=settings.analysis.report.title) if live else None

    recorder = TelemetryRecorder()
    registry = HandleRegistry(zone_map)
    backend = EnergyPlusBackend(install)
    pacing_seconds = settings.agent.live_pacing_seconds_per_timestep if live else 0.0

    def on_sample(sample: TelemetrySample) -> None:
        recorder.record(sample)
        telemetry_bus.put_nowait(sample)
        if pacing_seconds:
            time.sleep(pacing_seconds)

    callbacks = ReflexCallbacks(backend, zone_map, registry, on_sample, reflex_controller=reflex)

    started_at = datetime.now(UTC)
    registry.request_all(backend)
    callbacks.register()
    worker.start()
    if dashboard is not None:
        dashboard.start()
    _logger.info(
        "starting run",
        controller="agent",
        profile=profile,
        idf=str(prepared_idf),
        output_dir=str(resolved_output_dir),
    )
    # A cognitive cycle can legitimately make up to max_tool_calls_per_invocation
    # LLM round trips, each bounded by request_timeout_seconds - an in-flight
    # cycle deserves that much time to finish cleanly rather than being
    # abandoned on an arbitrary short timeout after EnergyPlus itself
    # (routinely much faster than a real LLM call) has already finished.
    worker_stop_timeout = (
        settings.agent.max_tool_calls_per_invocation * settings.llm.request_timeout_seconds
    )
    try:
        exit_code = backend.run(
            idf_path=prepared_idf, weather_path=resolved_weather, output_dir=resolved_output_dir
        )
    finally:
        worker.stop(timeout_seconds=worker_stop_timeout)
        if dashboard is not None:
            dashboard.stop()
    ended_at = datetime.now(UTC)
    _logger.info("cognitive worker stopped", cycles_run=worker.cycles_run)

    return _finalize_run(
        controller="agent",
        profile=profile,
        settings=settings,
        install=install,
        started_at=started_at,
        ended_at=ended_at,
        prepared_idf=prepared_idf,
        resolved_weather=resolved_weather,
        resolved_output_dir=resolved_output_dir,
        recorder=recorder,
        exit_code=exit_code,
        dropped_samples=telemetry_bus.dropped_count,
    )


def _finalize_run(
    *,
    controller: str,
    profile: str,
    settings: EcoLoopSettings,
    install: EnergyPlusInstall,
    started_at: datetime,
    ended_at: datetime,
    prepared_idf: Path,
    resolved_weather: Path,
    resolved_output_dir: Path,
    recorder: TelemetryRecorder,
    exit_code: int,
    dropped_samples: int,
) -> RunManifest:
    """Parse the finished run's ``.err``/``.eio``, persist telemetry, and write a manifest.

    Shared tail for both :func:`run_controller` and :func:`run_agent_controller`,
    since everything after EnergyPlus's ``run()`` returns is identical
    regardless of which controller drove the run.
    """
    err_path = resolved_output_dir / "eplusout.err"
    try:
        summary = parse_err_file(err_path, max_bytes=settings.simulation.output.max_err_bytes)
        succeeded = summary.completed_successfully
    except EcoLoopError:
        _logger.exception("could not parse .err file after run", err_path=str(err_path))
        succeeded = False

    telemetry_path = resolved_output_dir / "telemetry.parquet"
    recorder.write(telemetry_path)

    floor_area: float | None = None
    try:
        floor_area = conditioned_floor_area_m2(resolved_output_dir / "eplusout.eio")
    except EcoLoopError:
        _logger.exception(
            "could not compute conditioned floor area after run",
            output_dir=str(resolved_output_dir),
        )

    manifest = RunManifest(
        controller=controller,
        profile=profile,
        energyplus_version=install.version_string,
        started_at=started_at,
        ended_at=ended_at,
        idf_path=prepared_idf,
        weather_path=resolved_weather,
        output_dir=resolved_output_dir,
        telemetry_path=telemetry_path,
        timesteps_published=len(recorder),
        dropped_samples=dropped_samples,
        exit_code=exit_code,
        succeeded=succeeded,
        conditioned_floor_area_m2=floor_area,
    )
    manifest.write(resolved_output_dir / "manifest.json")
    _logger.info(
        "run complete",
        controller=controller,
        succeeded=succeeded,
        timesteps=len(recorder),
        output_dir=str(resolved_output_dir),
    )
    return manifest


def _timestamp_label() -> str:
    """A filesystem-safe, sortable timestamp for a run's output directory."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
