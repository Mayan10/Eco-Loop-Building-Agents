"""The LLM's only actuation surface (AGENTS.md invariant #3).

Both tools here do the same thing underneath: validate the proposal's shape,
build a :class:`~ecoloop.bus.policy.ControlPolicy`, and publish it to
:class:`~ecoloop.bus.policy.PolicyStore`. Nothing in this module enforces
safety — that happens later, in the reflex tier, via
:mod:`ecoloop.control.guardrails`, regardless of what gets published here
(AGENTS.md invariant #2). What this module *does* enforce is shape: a zone
name that does not exist in ``config/zones.yaml`` is rejected outright rather
than silently accepted and later failing to find an actuator for it.
"""

from __future__ import annotations

from ecoloop.bus.policy import ControlPolicy, PolicySource, ZoneSetpoint
from ecoloop.mcp.models import PolicyProposalResult, ZoneSetpointProposal
from ecoloop.mcp.state import ServerState

__all__ = ["propose_policy", "request_zone_setpoint"]


def propose_policy(
    state: ServerState,
    zone_setpoints: list[ZoneSetpointProposal],
    reasoning: str,
    ttl_minutes: float | None = None,
    lighting_fraction: float | None = None,
) -> PolicyProposalResult:
    """Publish a multi-zone control policy for the reflex tier to clamp and actuate.

    Args:
        state: Server state.
        zone_setpoints: Proposed heating/cooling setpoints, one entry per zone.
        reasoning: Free-text explanation, carried into the trace and the
            policy itself for later "why did you do that" questions. Treated
            as untrusted display text, never interpreted.
        ttl_minutes: How long this policy stays valid. Defaults to
            ``bus.policy.default_ttl_minutes`` if omitted.
        lighting_fraction: Optional global lighting-schedule override,
            0.0-1.0.

    Returns:
        Whether the policy was accepted, its id if so, and any zone names
        that were rejected for not existing in the active zone map.
    """
    sample = state.telemetry.latest()
    if sample is None:
        return PolicyProposalResult(
            accepted=False,
            policy_id=None,
            rejected_zones=(),
            message="no active simulation - nothing to actuate against",
        )

    valid_names = (
        {z.name.strip().upper() for z in state.zone_map.zones}
        if state.zone_map is not None
        else set()
    )
    accepted_proposals = [p for p in zone_setpoints if p.zone.strip().upper() in valid_names]
    rejected = tuple(p.zone for p in zone_setpoints if p.zone.strip().upper() not in valid_names)

    if not accepted_proposals:
        return PolicyProposalResult(
            accepted=False,
            policy_id=None,
            rejected_zones=rejected,
            message="no valid zones in proposal",
        )

    policy = ControlPolicy(
        issued_at=sample.clock,
        source=PolicySource.AGENT,
        ttl_minutes=ttl_minutes or state.policy.default_ttl_minutes,
        zone_setpoints=tuple(
            ZoneSetpoint(
                zone=p.zone,
                heating_setpoint_c=p.heating_setpoint_c,
                cooling_setpoint_c=p.cooling_setpoint_c,
            )
            for p in accepted_proposals
        ),
        lighting_fraction=lighting_fraction,
        reasoning=reasoning,
    )
    state.policy.publish(policy)

    message = f"published policy {policy.policy_id} for {len(accepted_proposals)} zone(s)"
    if rejected:
        message += f"; rejected unknown zones: {', '.join(rejected)}"

    if state.trace is not None:
        state.trace.record(
            tool="propose_policy",
            arguments={"zones": [p.zone for p in accepted_proposals], "reasoning": reasoning},
            result_summary=message,
            sim_clock=sample.clock,
        )

    return PolicyProposalResult(
        accepted=True, policy_id=policy.policy_id, rejected_zones=rejected, message=message
    )


def request_zone_setpoint(
    state: ServerState,
    zone: str,
    reasoning: str,
    heating_c: float | None = None,
    cooling_c: float | None = None,
    ttl_minutes: float | None = None,
) -> PolicyProposalResult:
    """Propose a setpoint change for a single zone.

    A convenience wrapper over :func:`propose_policy` for the common case of
    adjusting one zone. Omitting either setpoint carries that zone's current
    value through unchanged, so a partial update ("just raise cooling by a
    degree") does not require looking up the other value first.

    Args:
        state: Server state.
        zone: The zone to adjust.
        reasoning: Free-text explanation for the trace.
        heating_c: New heating setpoint, or ``None`` to leave it unchanged.
        cooling_c: New cooling setpoint, or ``None`` to leave it unchanged.
        ttl_minutes: How long this policy stays valid.

    Returns:
        The same result shape as :func:`propose_policy`.
    """
    sample = state.telemetry.latest()
    if sample is None:
        return PolicyProposalResult(
            accepted=False,
            policy_id=None,
            rejected_zones=(),
            message="no active simulation - nothing to actuate against",
        )

    current = sample.zone(zone)
    if current is None:
        return PolicyProposalResult(
            accepted=False,
            policy_id=None,
            rejected_zones=(zone,),
            message=f"zone '{zone}' not found",
        )

    proposal = ZoneSetpointProposal(
        zone=zone,
        heating_setpoint_c=heating_c if heating_c is not None else current.heating_setpoint_c,
        cooling_setpoint_c=cooling_c if cooling_c is not None else current.cooling_setpoint_c,
    )
    return propose_policy(state, [proposal], reasoning=reasoning, ttl_minutes=ttl_minutes)
