"""Hard safety clamps, enforced in code and applied to every proposal alike.

AGENTS.md invariant #2: guardrails are enforced here, never in a prompt. This
module has no notion of *who* proposed a setpoint — the LLM, the rule-based
controller, or the baseline's fixed schedule all pass through the exact same
clamp before anything reaches an actuator, which is what makes "a compromised
or hallucinating model cannot drive a zone outside the envelope" true rather
than aspirational.

Deliberately plain functions and a plain dataclass, not Pydantic models: this
runs once per zone per timestep inside the reflex tier's sub-millisecond
budget (AGENTS.md architecture — Tier 1 is ``<1 ms, no I/O``), and Pydantic
validation overhead belongs at the boundaries this module's *callers* sit
behind, not repeated on every one of tens of thousands of timesteps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ecoloop.config import GuardrailSettings

__all__ = [
    "ClampResult",
    "ZoneActuationMemory",
    "check_zone_temp_alarm",
    "clamp_lighting_fraction",
    "clamp_setpoints",
]


@dataclass(frozen=True, slots=True)
class ClampResult:
    """The setpoints actually safe to actuate, and why they differ from the proposal."""

    heating_setpoint_c: float
    cooling_setpoint_c: float
    violations: tuple[str, ...] = ()

    @property
    def was_clamped(self) -> bool:
        """Whether any guardrail altered the proposal."""
        return len(self.violations) > 0


@dataclass(slots=True)
class ZoneActuationMemory:
    """Per-zone state the rate-limit and hold-time guardrails need across timesteps.

    Owned by the caller (``control.reflex.ReflexController``) — one instance
    per zone, updated in place after every clamp. Absent any prior actuation
    (``last_heating_setpoint_c is None``), the first proposal always passes
    through unrated: there is no rate to limit and nothing to hold.
    """

    last_heating_setpoint_c: float | None = None
    last_cooling_setpoint_c: float | None = None
    minutes_since_change: float = field(default=0.0)

    def record(self, result: ClampResult, *, elapsed_minutes: float) -> None:
        """Update memory after a clamp, tracking whether the setpoints moved.

        Args:
            result: The clamp result just applied.
            elapsed_minutes: Simulated minutes since the previous call.
        """
        changed = (
            self.last_heating_setpoint_c != result.heating_setpoint_c
            or self.last_cooling_setpoint_c != result.cooling_setpoint_c
        )
        self.minutes_since_change = 0.0 if changed else self.minutes_since_change + elapsed_minutes
        self.last_heating_setpoint_c = result.heating_setpoint_c
        self.last_cooling_setpoint_c = result.cooling_setpoint_c


def _clamp_envelope(
    heating_c: float, cooling_c: float, guardrails: GuardrailSettings
) -> tuple[float, float, list[str]]:
    """Clamp both setpoints to their absolute min/max envelope.

    Args:
        heating_c: Proposed heating setpoint.
        cooling_c: Proposed cooling setpoint.
        guardrails: The active guardrail envelope.

    Returns:
        The clamped pair and a list of violation notes, empty if none.
    """
    violations: list[str] = []
    clamped_heating = heating_c
    clamped_cooling = cooling_c

    if not (guardrails.heating_setpoint_min_c <= heating_c <= guardrails.heating_setpoint_max_c):
        clamped_heating = min(
            max(heating_c, guardrails.heating_setpoint_min_c), guardrails.heating_setpoint_max_c
        )
        violations.append(f"heating_setpoint_c {heating_c} outside envelope, clamped")

    if not (guardrails.cooling_setpoint_min_c <= cooling_c <= guardrails.cooling_setpoint_max_c):
        clamped_cooling = min(
            max(cooling_c, guardrails.cooling_setpoint_min_c), guardrails.cooling_setpoint_max_c
        )
        violations.append(f"cooling_setpoint_c {cooling_c} outside envelope, clamped")

    return clamped_heating, clamped_cooling, violations


def _enforce_deadband(
    heating_c: float, cooling_c: float, guardrails: GuardrailSettings
) -> tuple[float, float, list[str]]:
    """Widen the gap between setpoints to at least the minimum deadband.

    Heating at or above cooling causes simultaneous heating and cooling,
    which *raises* energy use rather than saving it (AGENTS.md invariant #8's
    sibling landmine). Cooling is raised first to restore the deadband; if
    that would exceed the cooling envelope, heating is lowered instead, so
    the result never leaves the envelope :func:`_clamp_envelope` already
    established.

    Args:
        heating_c: Envelope-clamped heating setpoint.
        cooling_c: Envelope-clamped cooling setpoint.
        guardrails: The active guardrail settings.

    Returns:
        The adjusted pair and a list of violation notes, empty if none.
    """
    gap = cooling_c - heating_c
    if gap >= guardrails.min_deadband_c:
        return heating_c, cooling_c, []

    widened_cooling = heating_c + guardrails.min_deadband_c
    if widened_cooling <= guardrails.cooling_setpoint_max_c:
        return heating_c, widened_cooling, [f"deadband {gap:.2f}C too narrow, cooling raised"]

    widened_heating = guardrails.cooling_setpoint_max_c - guardrails.min_deadband_c
    return (
        widened_heating,
        guardrails.cooling_setpoint_max_c,
        [f"deadband {gap:.2f}C too narrow, heating lowered"],
    )


def _enforce_rate_and_hold(
    heating_c: float,
    cooling_c: float,
    memory: ZoneActuationMemory,
    elapsed_minutes: float,
    guardrails: GuardrailSettings,
) -> tuple[float, float, list[str]]:
    """Cap the rate of change and refuse changes inside the minimum hold time.

    Two different clocks are in play here, and conflating either pair is a
    bug: the **hold** check needs "how long will the currently-applied value
    have been in effect once this timestep lands" — ``memory.minutes_since_change``
    only accounts for time up to the *previous* call, since it is
    :meth:`ZoneActuationMemory.record` (called after this function returns)
    that folds in the current tick's duration. Evaluating the hold check
    against the stale, not-yet-updated figure means a value that has truly
    been stable for, say, 40 minutes gets refused as if it had been stable
    for 0 — so this function *projects* ``memory.minutes_since_change``
    forward by ``elapsed_minutes`` before checking it. The **rate cap**, by
    contrast, genuinely wants just ``elapsed_minutes`` — one control-tick's
    duration — not the cumulative time since the last real change; using the
    latter would let a setpoint that has sat still for two hours jump twice
    as far in a single tick as one that changed a minute ago.

    Args:
        heating_c: Deadband-adjusted heating setpoint.
        cooling_c: Deadband-adjusted cooling setpoint.
        memory: This zone's actuation history.
        elapsed_minutes: Simulated minutes since the previous call to
            :func:`clamp_setpoints` for this zone, regardless of whether that
            call changed anything.
        guardrails: The active guardrail settings.

    Returns:
        The rate/hold-adjusted pair and a list of violation notes.
    """
    if memory.last_heating_setpoint_c is None or memory.last_cooling_setpoint_c is None:
        return heating_c, cooling_c, []

    violations: list[str] = []
    projected_minutes_since_change = memory.minutes_since_change + elapsed_minutes

    if projected_minutes_since_change < guardrails.min_hold_minutes and (
        heating_c != memory.last_heating_setpoint_c or cooling_c != memory.last_cooling_setpoint_c
    ):
        violations.append(
            f"held at previous setpoints, {projected_minutes_since_change:.1f} min "
            f"< min_hold_minutes {guardrails.min_hold_minutes}"
        )
        return memory.last_heating_setpoint_c, memory.last_cooling_setpoint_c, violations

    max_delta = guardrails.max_setpoint_change_per_hour_c * (elapsed_minutes / 60.0)
    limited_heating = _limit_delta(heating_c, memory.last_heating_setpoint_c, max_delta)
    limited_cooling = _limit_delta(cooling_c, memory.last_cooling_setpoint_c, max_delta)
    if limited_heating != heating_c or limited_cooling != cooling_c:
        violations.append(f"rate-limited to {max_delta:.2f}C for this timestep")

    return limited_heating, limited_cooling, violations


def _limit_delta(proposed: float, previous: float, max_delta: float) -> float:
    """Cap how far ``proposed`` may move from ``previous`` in one step.

    Args:
        proposed: The desired value.
        previous: The last applied value.
        max_delta: Maximum allowed absolute change.

    Returns:
        ``proposed`` if within ``max_delta`` of ``previous``, otherwise the
        nearest value that is.
    """
    delta = proposed - previous
    if abs(delta) <= max_delta:
        return proposed
    return previous + max_delta if delta > 0 else previous - max_delta


def clamp_setpoints(
    *,
    proposed_heating_c: float,
    proposed_cooling_c: float,
    memory: ZoneActuationMemory,
    elapsed_minutes: float,
    guardrails: GuardrailSettings,
) -> ClampResult:
    """Clamp a proposed setpoint pair through the full guardrail chain.

    Order matters: the absolute envelope is applied first (nothing may ever
    leave it), then the deadband is restored within that envelope, then the
    rate limit and minimum hold time are applied relative to the zone's
    actuation history. The deadband is then **re-checked**: rate-limiting
    caps heating and cooling independently, each relative to its own
    previous value, and a tight-but-valid previous pair combined with a
    proposal pulling heating up while pulling cooling down can shrink the
    gap below the minimum even though every individual stage upheld it in
    isolation. Re-enforcing the deadband as the final step guarantees the
    output satisfies it regardless of what any earlier stage produced,
    rather than relying on a particular guardrail configuration's numbers
    happening to make that impossible.

    Args:
        proposed_heating_c: The heating setpoint some controller wants.
        proposed_cooling_c: The cooling setpoint some controller wants.
        memory: This zone's actuation history. Not mutated here — the caller
            updates it via :meth:`ZoneActuationMemory.record` once the clamp
            result is actually applied.
        elapsed_minutes: Simulated minutes since the previous call for this
            zone, regardless of whether that call changed anything. Drives
            the rate cap; see :func:`_enforce_rate_and_hold` for why this
            must not be confused with ``memory.minutes_since_change``.
        guardrails: The active guardrail settings.

    Returns:
        The setpoints that are actually safe to actuate this timestep.
    """
    violations: list[str] = []

    heating_c, cooling_c, envelope_violations = _clamp_envelope(
        proposed_heating_c, proposed_cooling_c, guardrails
    )
    violations.extend(envelope_violations)

    heating_c, cooling_c, deadband_violations = _enforce_deadband(heating_c, cooling_c, guardrails)
    violations.extend(deadband_violations)

    heating_c, cooling_c, rate_violations = _enforce_rate_and_hold(
        heating_c, cooling_c, memory, elapsed_minutes, guardrails
    )
    violations.extend(rate_violations)

    # The hold branch of _enforce_rate_and_hold returns memory's stored
    # values verbatim, trusting that they came from a prior valid clamp. That
    # trust is normally warranted, but a corrupted or externally-constructed
    # ZoneActuationMemory should not be able to smuggle an out-of-envelope
    # value past this function just because it matched "no change" — so the
    # envelope (and, following it, the deadband) is re-checked one final time
    # regardless of which path produced the pre-final value.
    heating_c, cooling_c, final_envelope_violations = _clamp_envelope(
        heating_c, cooling_c, guardrails
    )
    violations.extend(final_envelope_violations)

    heating_c, cooling_c, final_deadband_violations = _enforce_deadband(
        heating_c, cooling_c, guardrails
    )
    violations.extend(final_deadband_violations)

    return ClampResult(
        heating_setpoint_c=heating_c,
        cooling_setpoint_c=cooling_c,
        violations=tuple(violations),
    )


def check_zone_temp_alarm(air_temperature_c: float, guardrails: GuardrailSettings) -> str | None:
    """Flag a zone air temperature outside the hard alarm band.

    This is an observation, not a clamp: nothing here controls zone air
    temperature directly, only setpoints, so an alarm cannot be "fixed" by
    this function — it exists purely to surface a physically dangerous
    reading regardless of what any policy proposed.

    Args:
        air_temperature_c: Measured zone air temperature.
        guardrails: The active guardrail settings.

    Returns:
        A description of the alarm, or ``None`` if the temperature is within
        bounds.
    """
    if air_temperature_c < guardrails.zone_temp_alarm_min_c:
        return f"zone air temperature {air_temperature_c}C below alarm floor"
    if air_temperature_c > guardrails.zone_temp_alarm_max_c:
        return f"zone air temperature {air_temperature_c}C above alarm ceiling"
    return None


def clamp_lighting_fraction(
    proposed_fraction: float, *, occupied: bool, guardrails: GuardrailSettings
) -> float:
    """Clamp a proposed lighting fraction against the occupied load-shed floor.

    Args:
        proposed_fraction: Desired fraction of full lighting output, in
            ``[0.0, 1.0]``.
        occupied: Whether the zone is currently occupied.
        guardrails: The active guardrail settings.

    Returns:
        The proposed fraction, floored at
        ``min_lighting_fraction_occupied`` while occupied. Unoccupied zones
        are not floored — dimming or shutting off lights nobody is using is
        exactly the kind of saving this project exists to find.
    """
    clamped = max(0.0, min(1.0, proposed_fraction))
    if occupied:
        return max(clamped, guardrails.min_lighting_fraction_occupied)
    return clamped
