"""Compare multiple runs' energy and comfort metrics side by side.

A comparison is only meaningful between runs that faced the same building,
weather, run period, and engine — otherwise a difference in kWh measures
nothing about the controller. :func:`compare_runs` refuses (raises
:class:`~ecoloop.errors.UnfairComparisonError`) rather than silently
reporting a number if any of those "experimental fingerprint" fields differ
across the runs being compared.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ecoloop.analysis.collect import read_telemetry
from ecoloop.analysis.comfort import ComfortMetrics, compute_comfort_metrics
from ecoloop.analysis.metrics import EnergyMetrics, compute_energy_metrics
from ecoloop.config import EcoLoopSettings
from ecoloop.errors import UnfairComparisonError
from ecoloop.logging import get_logger
from ecoloop.runner import RunManifest

__all__ = ["ComparisonEntry", "ComparisonResult", "compare_runs", "find_latest_runs"]

_logger = get_logger(__name__, component="analysis")


class ComparisonEntry(BaseModel):
    """One run's manifest and computed metrics, as part of a comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: RunManifest
    energy: EnergyMetrics
    comfort: ComfortMetrics


class ComparisonResult(BaseModel):
    """A side-by-side comparison of two or more runs sharing a fingerprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    weather_path: Path
    energyplus_version: str
    entries: tuple[ComparisonEntry, ...]

    def entry(self, controller: str) -> ComparisonEntry | None:
        """Look up one controller's entry by name.

        Args:
            controller: Controller name, e.g. ``"baseline"``.

        Returns:
            The matching entry, or ``None`` if no run for that controller
            is part of this comparison.
        """
        return next((e for e in self.entries if e.manifest.controller == controller), None)


def compare_runs(run_dirs: list[Path], settings: EcoLoopSettings) -> ComparisonResult:
    """Load each run's manifest and telemetry, and compare them.

    Args:
        run_dirs: Output directories, each containing a ``manifest.json``
            written by :func:`ecoloop.runner.run_controller`.
        settings: Loaded Eco-Loop settings, for metric computation.

    Returns:
        A comparison bundling every run's manifest and computed metrics.

    Raises:
        UnfairComparisonError: If the runs do not share the same profile,
            weather file, and EnergyPlus version.
        ValueError: If ``run_dirs`` is empty.
    """
    if not run_dirs:
        raise ValueError("compare_runs needs at least one run directory")

    manifests = [
        RunManifest.model_validate_json((d / "manifest.json").read_text(encoding="utf-8"))
        for d in run_dirs
    ]
    _check_fair_comparison(manifests)

    entries = tuple(
        ComparisonEntry(
            manifest=manifest,
            energy=compute_energy_metrics(
                read_telemetry(manifest.telemetry_path),
                settings,
                conditioned_floor_area_m2=manifest.conditioned_floor_area_m2,
            ),
            comfort=compute_comfort_metrics(read_telemetry(manifest.telemetry_path), settings),
        )
        for manifest in manifests
    )
    return ComparisonResult(
        profile=manifests[0].profile,
        weather_path=manifests[0].weather_path,
        energyplus_version=manifests[0].energyplus_version,
        entries=entries,
    )


def _check_fair_comparison(manifests: list[RunManifest]) -> None:
    """Raise if these runs did not face the same weather, period, or engine.

    ``idf_path`` is deliberately not part of this check: every run overwrites
    the same ``simulation.idf_prepared`` destination, so the path is always
    identical across controllers regardless of what was actually simulated -
    it would validate nothing.
    """
    first = manifests[0]
    for other in manifests[1:]:
        mismatched = {
            field: (getattr(first, field), getattr(other, field))
            for field in ("profile", "weather_path", "energyplus_version")
            if getattr(first, field) != getattr(other, field)
        }
        if mismatched:
            raise UnfairComparisonError(
                "runs do not share the same experimental fingerprint",
                controllers=f"{first.controller!r} vs {other.controller!r}",
                mismatched=str(mismatched),
            )


def find_latest_runs(results_root: Path) -> dict[str, Path]:
    """Find the most recently started run directory for each controller.

    Args:
        results_root: The runs root, conventionally ``results/runs``, laid
            out as ``<profile>/<controller>/<timestamp>/manifest.json``.

    Returns:
        A mapping from controller name to its latest run's output directory.
        Empty if no manifests are found anywhere under ``results_root``.
    """
    latest: dict[str, tuple[RunManifest, Path]] = {}
    for manifest_path in results_root.glob("*/*/*/manifest.json"):
        try:
            manifest = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _logger.warning("skipping unreadable manifest", path=str(manifest_path))
            continue
        current = latest.get(manifest.controller)
        if current is None or manifest.started_at > current[0].started_at:
            latest[manifest.controller] = (manifest, manifest_path.parent)
    return {controller: run_dir for controller, (_, run_dir) in latest.items()}
