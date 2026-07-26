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

**Deliberately scoped to the synchronous controllers.** ``baseline`` and
``rulebased`` compute a policy in the callback itself, with no latency to
hide — so running them needs nothing beyond this module. ``agent`` reads from
a :class:`~ecoloop.bus.policy.PolicyStore` that only a worker thread running
the cognitive tier's cadence loop can keep current; wiring that thread
alongside EnergyPlus's blocking, main-thread-owning ``run()`` call is a
distinct integration task (see ``agent/AGENTS.md``), not attempted here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from ecoloop.analysis.collect import TelemetryRecorder
from ecoloop.config import EcoLoopSettings
from ecoloop.control.reflex import ReflexController
from ecoloop.errors import EcoLoopError
from ecoloop.logging import get_logger
from ecoloop.simulation.callbacks import ReflexCallbacks
from ecoloop.simulation.eio import conditioned_floor_area_m2
from ecoloop.simulation.energyplus import EnergyPlusBackend
from ecoloop.simulation.errfile import parse_err_file
from ecoloop.simulation.handles import HandleRegistry, load_zone_map
from ecoloop.simulation.idf import set_idd
from ecoloop.simulation.locate import discover_energyplus
from ecoloop.simulation.prepare import prepare_idf

__all__ = ["RunManifest", "SynchronousController", "run_controller"]

_logger = get_logger(__name__, component="runner")

SynchronousController = Literal["baseline", "rulebased"]
_SYNCHRONOUS_CONTROLLERS: tuple[str, ...] = get_args(SynchronousController)


class RunManifest(BaseModel):
    """A record of one completed (or failed) run, for `compare`/`report` to consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    controller: str
    profile: str
    started_at: datetime
    ended_at: datetime
    idf_path: Path
    weather_path: Path
    output_dir: Path
    telemetry_path: Path
    timesteps_published: int
    dropped_samples: int
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
        started_at=started_at,
        ended_at=ended_at,
        idf_path=prepared_idf,
        weather_path=resolved_weather,
        output_dir=resolved_output_dir,
        telemetry_path=telemetry_path,
        timesteps_published=len(recorder),
        dropped_samples=0,
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
