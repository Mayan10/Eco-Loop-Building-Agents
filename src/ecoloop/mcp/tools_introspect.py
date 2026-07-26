"""Introspection tools: what has already happened, and why.

Two of these — :func:`list_sandboxed_files` and :func:`read_sandboxed_text_file`
— are the only tools in the project that accept an arbitrary path from the
LLM. Every path they touch goes through
:func:`~ecoloop.mcp.sandbox.resolve_sandboxed_path` first (AGENTS.md N3.3),
and file content returned from :func:`read_sandboxed_text_file` is untrusted
input (AGENTS.md N3.2): control characters are stripped and size is capped
before it ever reaches a prompt.
"""

from __future__ import annotations

from ecoloop.config import PROJECT_ROOT
from ecoloop.errors import SandboxViolationError
from ecoloop.mcp.models import (
    ActivePolicyResult,
    ErrorRecordResult,
    GuardrailViolationResult,
    RunManifestResult,
)
from ecoloop.mcp.sandbox import resolve_sandboxed_path
from ecoloop.mcp.state import ServerState
from ecoloop.simulation.errfile import Severity, parse_err_file

__all__ = [
    "get_active_policy",
    "get_guardrail_violations",
    "get_recent_errors",
    "get_run_manifest",
    "get_run_period_info",
    "list_sandboxed_files",
    "read_sandboxed_text_file",
]


def get_active_policy(state: ServerState) -> ActivePolicyResult:
    """The currently active control policy, if one exists and has not expired.

    Args:
        state: Server state.

    Returns:
        The active policy's source, age, TTL, reasoning, and zone setpoints,
        or an "inactive" result if none is current.
    """
    sample = state.telemetry.latest()
    if sample is None:
        return ActivePolicyResult(
            has_active_policy=False,
            source=None,
            age_minutes=None,
            ttl_minutes=None,
            reasoning=None,
            zone_setpoints={},
        )

    policy = state.policy.current(sample.clock)
    if policy is None:
        return ActivePolicyResult(
            has_active_policy=False,
            source=None,
            age_minutes=None,
            ttl_minutes=None,
            reasoning=None,
            zone_setpoints={},
        )

    return ActivePolicyResult(
        has_active_policy=True,
        source=str(policy.source),
        age_minutes=policy.age_minutes(sample.clock),
        ttl_minutes=policy.ttl_minutes,
        reasoning=policy.reasoning,
        zone_setpoints={
            z.zone: (z.heating_setpoint_c, z.cooling_setpoint_c) for z in policy.zone_setpoints
        },
    )


def get_guardrail_violations(
    state: ServerState, count: int = 10
) -> tuple[GuardrailViolationResult, ...]:
    """Recent guardrail interventions, newest first.

    Args:
        state: Server state.
        count: Maximum number of records to return.

    Returns:
        Up to ``count`` recent violations. Empty if no reflex controller is
        attached to this server (no simulation running) or nothing has ever
        been clamped.
    """
    if state.reflex is None:
        return ()
    return tuple(
        GuardrailViolationResult(
            sim_clock_iso=record.sim_clock.isoformat(),
            zone=record.zone,
            violations=record.violations,
        )
        for record in state.reflex.recent_violations(count)
    )


def get_run_manifest(state: ServerState) -> RunManifestResult:
    """Run-level metadata: profile, controller mode, and telemetry health.

    Args:
        state: Server state.

    Returns:
        A summary an agent can use to orient itself at the start of a session.
    """
    zone_names = (
        tuple(z.name for z in state.zone_map.zones if z.conditioned)
        if state.zone_map is not None
        else ()
    )
    return RunManifestResult(
        profile=state.settings.profile or "default",
        controller=state.settings.control.controller,
        published_samples=state.telemetry.published_count,
        dropped_samples=state.telemetry.dropped_count,
        zones_conditioned=zone_names,
        run_period=state.settings.simulation.run_period.describe(),
    )


def get_run_period_info(state: ServerState) -> str:
    """Describe the active run period and profile in one human-readable line.

    Args:
        state: Server state.

    Returns:
        E.g. ``"profile=fast period=07-15..07-28 timesteps_per_hour=6"``.
    """
    run_period = state.settings.simulation.run_period
    return (
        f"profile={state.settings.profile} period={run_period.describe()} "
        f"timesteps_per_hour={state.settings.simulation.timesteps_per_hour} "
        f"annual={run_period.is_annual}"
    )


def get_recent_errors(
    state: ServerState, min_severity: str = "warning", count: int = 10
) -> tuple[ErrorRecordResult, ...]:
    """The most recent EnergyPlus ``.err`` records at or above a severity.

    Args:
        state: Server state.
        min_severity: One of ``"info"``, ``"warning"``, ``"severe"``,
            ``"fatal"``. Records below this are excluded.
        count: Maximum number of records to return, most recent first.

    Returns:
        Matching records. Empty if no ``.err`` file is attached to this run.
    """
    if state.err_path is None or not state.err_path.is_file():
        return ()

    severities = list(Severity)
    try:
        threshold_index = severities.index(Severity(min_severity.lower()))
    except ValueError:
        threshold_index = severities.index(Severity.WARNING)

    max_bytes = state.settings.simulation.output.max_err_bytes
    summary = parse_err_file(state.err_path, max_bytes=max_bytes)
    matching = [r for r in summary.records if severities.index(r.severity) >= threshold_index]
    return tuple(
        ErrorRecordResult(severity=str(r.severity), message=r.message, line_number=r.line_number)
        for r in reversed(matching[-count:])
    )


def list_sandboxed_files(state: ServerState, subdirectory: str = "") -> tuple[str, ...]:
    """List files under an allowlisted sandbox root.

    Args:
        state: Server state.
        subdirectory: A path relative to the project root, e.g. ``"models"``
            or ``"config/signals"``. Must resolve inside one of
            ``mcp.sandbox_roots``.

    Returns:
        Relative file paths found under that directory, or an empty tuple if
        the path is outside every sandbox root or does not exist.
    """
    try:
        resolved = resolve_sandboxed_path(
            subdirectory or ".", roots=state.sandbox_roots, project_root=PROJECT_ROOT
        )
    except SandboxViolationError:
        return ()
    if not resolved.is_dir():
        return ()
    return tuple(sorted(str(p.relative_to(resolved)) for p in resolved.rglob("*") if p.is_file()))


def read_sandboxed_text_file(state: ServerState, path: str, max_bytes: int = 65_536) -> str:
    """Read a text file from an allowlisted sandbox root.

    The returned content is untrusted input once it reaches a prompt
    (AGENTS.md N3.2): control characters are stripped here so a malformed or
    adversarial file cannot inject terminal escapes or similar into a
    downstream context window.

    Args:
        state: Server state.
        path: A path relative to the project root. Must resolve inside one
            of ``mcp.sandbox_roots``; ``..`` segments, absolute escapes, and
            symlinks resolving outside the sandbox are all rejected.
        max_bytes: Maximum bytes to read.

    Returns:
        The file's content, control characters stripped, or an explanatory
        message if the path is invalid or outside the sandbox.
    """
    try:
        resolved = resolve_sandboxed_path(
            path, roots=state.sandbox_roots, project_root=PROJECT_ROOT
        )
    except SandboxViolationError as exc:
        return f"error: {exc}"
    if not resolved.is_file():
        return f"error: not a file: {path}"

    raw = resolved.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    return "".join(ch for ch in raw if ch in "\n\t" or ch.isprintable())
