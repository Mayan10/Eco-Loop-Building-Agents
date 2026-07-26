"""Environment diagnostics — the first thing to run in a fresh checkout.

``ecoloop doctor`` answers one question: *can this machine actually run the
closed loop, and if not, exactly what do I type to fix it?* Every failed check
carries a copy-pasteable remediation rather than a stack trace.

Checks are grouped by severity:

* **Required** — the closed loop cannot run without these.
* **Optional** — degraded but usable (e.g. the agent falls back to rule-based
  control when no LLM is reachable).

Exit status is non-zero only when a *required* check fails, so ``doctor`` is
usable as a CI gate.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from ecoloop.config import EcoLoopSettings
from ecoloop.errors import EcoLoopError
from ecoloop.simulation.locate import discover_energyplus, import_energyplus_api, install_hint

__all__ = ["CheckResult", "Status", "run_checks"]

_MIN_PYTHON = (3, 11)
_BYTES_PER_GIB = 1024**3


class Status(StrEnum):
    """Outcome of a single diagnostic check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The result of one diagnostic check.

    Attributes:
        name: Short label, e.g. ``"EnergyPlus"``.
        status: Whether the check passed, warned, or failed.
        detail: What was found. Shown on every outcome.
        remediation: Copy-pasteable fix. Shown only on WARN or FAIL.
        required: Whether a failure here blocks the closed loop.
    """

    name: str
    status: Status
    detail: str
    remediation: str | None = None
    required: bool = True

    @property
    def blocking(self) -> bool:
        """Whether this result should make ``doctor`` exit non-zero."""
        return self.required and self.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def _check_python() -> CheckResult:
    """Verify the interpreter meets the minimum version."""
    current = sys.version_info[:3]
    rendered = ".".join(str(part) for part in current)
    if current[:2] < _MIN_PYTHON:
        needed = ".".join(str(part) for part in _MIN_PYTHON)
        return CheckResult(
            name="Python",
            status=Status.FAIL,
            detail=f"{rendered} on {platform.machine()} — too old",
            remediation=f"Eco-Loop requires Python >= {needed}. Try: uv venv --python 3.12",
        )
    return CheckResult(
        name="Python",
        status=Status.OK,
        detail=f"{rendered} ({platform.python_implementation()} on {platform.machine()})",
    )


def _check_energyplus(settings: EcoLoopSettings | None) -> Iterator[CheckResult]:
    """Locate EnergyPlus, verify its version, and confirm the API imports."""
    explicit = settings.simulation.energyplus_dir if settings else None
    try:
        install = discover_energyplus(explicit)
    except EcoLoopError as exc:
        yield CheckResult(
            name="EnergyPlus",
            status=Status.FAIL,
            detail=str(exc.message).splitlines()[0],
            remediation=install_hint(),
        )
        return

    minimum = settings.simulation.min_version if settings else "23.2.0"
    maximum = settings.simulation.max_version if settings else "26.99.0"
    if not install.supports(minimum, maximum):
        yield CheckResult(
            name="EnergyPlus",
            status=Status.FAIL,
            detail=f"version {install.version_string} at {install.root}",
            remediation=(
                f"Supported range is {minimum} … {maximum}. The Python API surface differs "
                "between major versions; install a supported release or widen "
                "simulation.min_version / max_version if you have verified compatibility."
            ),
        )
        return

    yield CheckResult(
        name="EnergyPlus",
        status=Status.OK,
        detail=f"{install.version_string} at {install.root} (via {install.source})",
    )

    # pyenergyplus is not on PyPI; confirm it genuinely imports from that root.
    try:
        api = import_energyplus_api(install)
        expected = ("exchange", "runtime", "state_manager")
        has_surface = all(hasattr(api, attr) for attr in expected)
    except EcoLoopError as exc:
        yield CheckResult(
            name="pyenergyplus",
            status=Status.FAIL,
            detail=str(exc.message).splitlines()[0],
            remediation=(
                "The EnergyPlus build and this Python interpreter likely target different "
                f"architectures (Python is {platform.machine()}). Reinstall the matching build."
            ),
        )
        return

    yield CheckResult(
        name="pyenergyplus",
        status=Status.OK if has_surface else Status.FAIL,
        detail=(
            "imports; exchange/runtime/state_manager present"
            if has_surface
            else "imports but the expected API surface is missing"
        ),
        remediation=None if has_surface else "Unsupported EnergyPlus build — try 25.2.0.",
    )


def _check_llm(settings: EcoLoopSettings | None) -> Iterator[CheckResult]:
    """Check the LLM endpoint is reachable and the configured model is pulled."""
    if settings is None:
        return
    base_url = settings.llm.base_url.rstrip("/")
    wanted = settings.llm.model
    fallback = settings.llm.fallback_model

    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        yield CheckResult(
            name="LLM endpoint",
            status=Status.FAIL,
            detail=f"{base_url} unreachable: {type(exc).__name__}",
            remediation=(
                "Start the server:  ollama serve\n"
                "  (or point llm.base_url at any OpenAI-compatible endpoint)\n"
                "  Without it the agent degrades to the rule-based controller — the "
                "simulation still completes, but there is no LLM to evaluate."
            ),
        )
        return

    available = {model.get("name", "") for model in payload.get("models", [])}
    yield CheckResult(
        name="LLM endpoint",
        status=Status.OK,
        detail=f"{base_url} reachable, {len(available)} model(s) available",
    )

    if _model_present(wanted, available):
        yield CheckResult(name="LLM model", status=Status.OK, detail=f"{wanted} is pulled")
    else:
        yield CheckResult(
            name="LLM model",
            status=Status.FAIL,
            detail=f"{wanted} is not pulled (have: {', '.join(sorted(available)) or 'none'})",
            remediation=f"ollama pull {wanted}",
        )

    if fallback and not _model_present(fallback, available):
        yield CheckResult(
            name="LLM fallback model",
            status=Status.WARN,
            detail=f"{fallback} is not pulled",
            remediation=(
                f"ollama pull {fallback}\n"
                "  Optional: used for fast iteration and as a degradation target when the "
                "primary model is unavailable."
            ),
            required=False,
        )


def _model_present(wanted: str, available: set[str]) -> bool:
    """Test whether a model tag is present, tolerating an implicit ``:latest``.

    Args:
        wanted: The configured model name.
        available: Model names reported by the endpoint.

    Returns:
        True when the model (or its ``:latest`` form) is available.
    """
    candidates = {wanted, f"{wanted}:latest", wanted.removesuffix(":latest")}
    return bool(candidates & available)


def _check_inputs(settings: EcoLoopSettings | None) -> Iterator[CheckResult]:
    """Verify the baseline IDF and weather file exist where config says."""
    if settings is None:
        return

    idf = settings.resolve(settings.simulation.idf_baseline)
    yield CheckResult(
        name="Baseline IDF",
        status=Status.OK if idf.is_file() else Status.FAIL,
        detail=str(idf) if idf.is_file() else f"missing: {idf}",
        remediation=None if idf.is_file() else "python scripts/fetch_model.py",
    )

    epw = settings.resolve(settings.simulation.weather)
    yield CheckResult(
        name="Weather (EPW)",
        status=Status.OK if epw.is_file() else Status.FAIL,
        detail=str(epw) if epw.is_file() else f"missing: {epw}",
        remediation=None if epw.is_file() else "python scripts/fetch_model.py",
    )


def _check_disk(settings: EcoLoopSettings | None) -> CheckResult:
    """Warn when free disk is below the configured floor for an annual run."""
    minimum = settings.project.min_free_disk_gib if settings else 5.0
    free_gib = shutil.disk_usage(Path.cwd()).free / _BYTES_PER_GIB
    enough = free_gib >= minimum
    return CheckResult(
        name="Disk space",
        status=Status.OK if enough else Status.WARN,
        detail=f"{free_gib:.1f} GiB free (annual runs want >= {minimum:.0f} GiB)",
        remediation=None if enough else "Free space or run `make clean` to drop old results.",
        required=False,
    )


def _check_git() -> CheckResult:
    """Report whether git is available for run-provenance stamping."""
    git = shutil.which("git")
    return CheckResult(
        name="git",
        status=Status.OK if git else Status.WARN,
        detail=git or "not found",
        remediation=None if git else "Install git so run manifests can record the commit SHA.",
        required=False,
    )


def _check_secrets() -> CheckResult:
    """Confirm no API key is set for an endpoint that does not need one."""
    leaked = [name for name in os.environ if name.endswith("_API_KEY") and os.environ[name]]
    return CheckResult(
        name="Secrets",
        status=Status.OK,
        detail=(
            f"{len(leaked)} *_API_KEY variable(s) set in the environment"
            if leaked
            else "no API keys needed for a local Ollama endpoint"
        ),
        required=False,
    )


def run_checks(settings: EcoLoopSettings | None) -> list[CheckResult]:
    """Run every diagnostic check.

    Args:
        settings: Loaded configuration. When ``None`` (because config itself
            failed to load) only environment-level checks run.

    Returns:
        Results in display order.
    """
    results: list[CheckResult] = [_check_python()]
    results.extend(_check_energyplus(settings))
    results.extend(_check_llm(settings))
    results.extend(_check_inputs(settings))
    results.append(_check_disk(settings))
    results.append(_check_git())
    results.append(_check_secrets())
    return results
