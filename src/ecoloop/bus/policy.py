"""The frozen policy object the worker publishes and the reflex tier reads.

``ControlPolicy`` is the other object that crosses the thread boundary. It is
frozen so a reflex callback that reads it once at the top of a timestep is
reading a single, internally-consistent decision for the whole timestep —
:class:`PolicyStore` swaps the *reference* atomically, never mutates a policy
in place, so there is no way to observe one half-updated.

Age is measured in **simulation time**, not wall-clock time. A run can
execute a two-week period in a couple of seconds or an annual period over
many minutes; "this policy is 90 minutes old" only means something if those
90 minutes are simulated building-time, matching the cadence the cognitive
layer actually reasons on.
"""

from __future__ import annotations

import threading
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ecoloop.bus.models import SimClock

__all__ = ["ControlPolicy", "PolicySource", "PolicyStore", "ZoneSetpoint"]


class PolicySource(StrEnum):
    """Who issued a policy, carried through to the trace and the report."""

    AGENT = "agent"
    RULEBASED = "rulebased"
    BASELINE = "baseline"
    DEFAULT = "default"


class ZoneSetpoint(BaseModel):
    """A proposed heating/cooling setpoint pair for one zone.

    These are *proposals*: the reflex tier clamps every field through
    ``control.guardrails`` before it ever reaches an actuator. Nothing in
    this model enforces the envelope, the deadband, or the rate limit —
    enforcing those here would be prompt-adjacent safety, and AGENTS.md
    invariant #2 is explicit that guardrails live in code that runs
    regardless of who proposed the value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    zone: str
    heating_setpoint_c: float
    cooling_setpoint_c: float


class ControlPolicy(BaseModel):
    """A complete, frozen control decision, valid until it expires.

    Args:
        policy_id: Opaque identifier for tracing a decision through logs.
        issued_at: Simulation clock at the moment this policy was created.
        source: Which controller produced this policy.
        ttl_minutes: Simulated minutes after which this policy is considered
            stale and the reflex tier degrades to the next rung.
        zone_setpoints: Proposed setpoints, one entry per zone this policy
            addresses. A zone with no entry keeps its previously-applied
            setpoint.
        lighting_fraction: Optional global lighting-schedule override.
        reasoning: Free-text explanation, carried into the agent trace for
            the "why did you raise the setpoint at 2pm" question. Untrusted
            free text from the LLM in agent-sourced policies — never
            interpreted as anything but a string to display.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    issued_at: SimClock
    source: PolicySource
    ttl_minutes: float = Field(gt=0.0)
    zone_setpoints: tuple[ZoneSetpoint, ...]
    lighting_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning: str = ""

    def zone(self, name: str) -> ZoneSetpoint | None:
        """Look up a proposed setpoint by zone name, case-insensitively.

        Args:
            name: Zone name in any casing.

        Returns:
            The matching proposal, or ``None`` if this policy has none for
            that zone.
        """
        wanted = name.strip().upper()
        return next((z for z in self.zone_setpoints if z.zone.strip().upper() == wanted), None)

    def age_minutes(self, now: SimClock) -> float:
        """How many simulated minutes have elapsed since this policy issued.

        Args:
            now: The current simulation clock.

        Returns:
            Elapsed simulated minutes. Negative if ``now`` precedes
            ``issued_at``, which should not happen in a forward-running
            simulation but is not this method's job to validate.
        """
        return (now.as_datetime - self.issued_at.as_datetime).total_seconds() / 60.0


class PolicyStore:
    """Holds the single active policy, written by the worker, read by reflex.

    Args:
        default_ttl_minutes: Fallback TTL, from ``bus.policy.default_ttl_minutes``.
        max_age_minutes: Hard ceiling on policy age regardless of the policy's
            own TTL, from ``bus.policy.max_age_minutes`` — defence in depth
            against a policy that set an anomalously long TTL for itself.
    """

    def __init__(self, *, default_ttl_minutes: float, max_age_minutes: float) -> None:
        """Create an empty store with the given fallback TTL and hard age ceiling."""
        self._lock = threading.Lock()
        self._policy: ControlPolicy | None = None
        self.default_ttl_minutes = default_ttl_minutes
        self.max_age_minutes = max_age_minutes

    def publish(self, policy: ControlPolicy) -> None:
        """Replace the active policy.

        Called from the worker thread. The store never mutates a policy in
        place — this swaps the reference under a lock, so a reflex callback
        reading concurrently always sees either the old policy or the new
        one, never a mix of both.

        Args:
            policy: The new policy to make active.
        """
        with self._lock:
            self._policy = policy

    def current(self, now: SimClock) -> ControlPolicy | None:
        """Read the active policy, if one exists and has not expired.

        Called from the reflex callback on the main thread. Expiry is
        checked against the *smaller* of the policy's own TTL and this
        store's ``max_age_minutes`` ceiling, so a policy cannot outlive the
        hard cap by declaring a long TTL for itself.

        Args:
            now: The current simulation clock.

        Returns:
            The active policy, or ``None`` if none has been published yet or
            the active one has expired. A ``None`` here is what "the reflex
            layer degrades to the rule-based controller" means in practice —
            it is not an error condition.
        """
        with self._lock:
            policy = self._policy
        if policy is None:
            return None
        effective_ttl = min(policy.ttl_minutes, self.max_age_minutes)
        if policy.age_minutes(now) > effective_ttl:
            return None
        return policy
