"""Tier 1: read a policy, clamp it through the guardrail chain, decide what to actuate.

``ReflexController`` is the piece of the two-tier architecture that runs
every timestep, in the EnergyPlus callback, with no I/O. Where the policy it
reads comes from depends on ``control.controller``:

* ``baseline`` / ``rulebased`` — computed synchronously, right here, every
  timestep. There is no latency to hide, so there is no reason to involve a
  worker thread or :class:`~ecoloop.bus.policy.PolicyStore` at all.
* ``agent`` — read from :class:`~ecoloop.bus.policy.PolicyStore`, published
  asynchronously by the Phase 6 cognitive worker. If the store has nothing
  current (never published, or expired), this degrades to the same
  rule-based computation the ``rulebased`` mode uses directly — the ladder
  AGENTS.md describes as "the reflex layer degrades to the rule-based
  controller, then to the building's native schedule."

Guardrail clamping (:mod:`ecoloop.control.guardrails`) happens after a policy
is resolved, regardless of which of the three rungs produced it. That
uniformity is the point: the guardrail chain does not know or care whether a
proposal came from an LLM, a heuristic, or a fixed schedule.
"""

from __future__ import annotations

from collections.abc import Sequence

from ecoloop.bus.models import SimClock, TelemetrySample
from ecoloop.bus.policy import ControlPolicy, PolicyStore
from ecoloop.config import ControlSettings, GuardrailSettings
from ecoloop.control.baseline import BaselineController
from ecoloop.control.guardrails import ClampResult, ZoneActuationMemory, clamp_setpoints
from ecoloop.control.rulebased import RuleBasedController
from ecoloop.logging import get_logger

__all__ = ["ReflexController"]

_logger = get_logger(__name__, component="reflex")


class ReflexController:
    """Combines controller selection, degradation, and guardrail clamping.

    Args:
        zone_names: Zones this controller actuates.
        control: Controller settings, including the active mode.
        guardrails: The hard safety envelope applied to every proposal.
        occupied_threshold_fraction: Fractional occupancy above which a zone
            counts as occupied, from ``comfort.occupied_threshold_fraction``.
        ttl_minutes: Policy TTL for the synchronous controllers, from
            ``bus.policy.default_ttl_minutes``.
        policy_store: The agent-published policy store. Required when
            ``control.controller == "agent"``; unused otherwise.
    """

    def __init__(
        self,
        *,
        zone_names: Sequence[str],
        control: ControlSettings,
        guardrails: GuardrailSettings,
        occupied_threshold_fraction: float,
        ttl_minutes: float,
        policy_store: PolicyStore | None = None,
    ) -> None:
        """Build the synchronous controllers and per-zone actuation memory."""
        self._zone_names = tuple(zone_names)
        self._controller = control.controller
        self._guardrails = guardrails
        self._policy_store = policy_store
        self._baseline = BaselineController(
            zone_names=zone_names,
            control=control,
            occupied_threshold_fraction=occupied_threshold_fraction,
            ttl_minutes=ttl_minutes,
        )
        self._rulebased = RuleBasedController(
            zone_names=zone_names,
            control=control,
            occupied_threshold_fraction=occupied_threshold_fraction,
            ttl_minutes=ttl_minutes,
        )
        self._memory: dict[str, ZoneActuationMemory] = {
            name: ZoneActuationMemory() for name in self._zone_names
        }
        self._last_decision_clock: SimClock | None = None

    def decide(self, sample: TelemetrySample) -> dict[str, ClampResult]:
        """Resolve a policy and clamp it into per-zone actuation values.

        Args:
            sample: The latest telemetry sample.

        Returns:
            A mapping of zone name to the setpoints actually safe to
            actuate this timestep. A zone absent from the mapping had no
            proposal this timestep (e.g. it is outside every controller's
            addressed set) and should be left on its current schedule.
        """
        policy = self._resolve_policy(sample)
        elapsed_minutes = self._elapsed_minutes(sample.clock)

        results: dict[str, ClampResult] = {}
        for name in self._zone_names:
            zone = sample.zone(name)
            proposal = policy.zone(name) if (zone is not None and policy is not None) else None
            if proposal is None:
                continue
            memory = self._memory[name]
            result = clamp_setpoints(
                proposed_heating_c=proposal.heating_setpoint_c,
                proposed_cooling_c=proposal.cooling_setpoint_c,
                memory=memory,
                elapsed_minutes=elapsed_minutes,
                guardrails=self._guardrails,
            )
            memory.record(result, elapsed_minutes=elapsed_minutes)
            results[name] = result

        self._last_decision_clock = sample.clock
        return results

    def _resolve_policy(self, sample: TelemetrySample) -> ControlPolicy | None:
        """Pick the active policy per the controller mode and degradation ladder.

        Args:
            sample: The latest telemetry sample.

        Returns:
            The policy to actuate from this timestep, or ``None`` if every
            rung failed — meaning the zone's current schedule is left alone.
        """
        if self._controller == "baseline":
            return self._baseline.decide(sample)
        if self._controller == "rulebased":
            return self._rulebased.decide(sample)

        if self._policy_store is not None:
            agent_policy = self._policy_store.current(sample.clock)
            if agent_policy is not None:
                return agent_policy

        try:
            return self._rulebased.decide(sample)
        except Exception:
            _logger.exception(
                "rule-based fallback failed while degrading from an expired "
                "or absent agent policy; leaving native schedule in effect"
            )
            return None

    def _elapsed_minutes(self, now: SimClock) -> float:
        """Simulated minutes since the previous call to :meth:`decide`.

        Args:
            now: The current simulation clock.

        Returns:
            Elapsed minutes, or ``0.0`` on the very first call.
        """
        if self._last_decision_clock is None:
            return 0.0
        return (now.as_datetime - self._last_decision_clock.as_datetime).total_seconds() / 60.0
