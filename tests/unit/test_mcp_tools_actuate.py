"""Tests for the LLM's only actuation surface."""

from __future__ import annotations

from _mcp_state_factory import make_sample, make_state, make_zone

from ecoloop.mcp import tools_actuate
from ecoloop.mcp.models import ZoneSetpointProposal


class TestProposePolicy:
    def test_rejects_when_no_simulation_is_active(self) -> None:
        state = make_state()
        result = tools_actuate.propose_policy(
            state,
            [
                ZoneSetpointProposal(
                    zone="CORE_ZN", heating_setpoint_c=21.0, cooling_setpoint_c=24.0
                )
            ],
            reasoning="test",
        )
        assert result.accepted is False
        assert "no active simulation" in result.message

    def test_accepts_a_valid_proposal_and_publishes_it(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_actuate.propose_policy(
            state,
            [
                ZoneSetpointProposal(
                    zone="CORE_ZN", heating_setpoint_c=19.0, cooling_setpoint_c=26.0
                )
            ],
            reasoning="precooling ahead of peak",
        )
        assert result.accepted is True
        assert result.policy_id is not None

        published = state.policy.current(state.telemetry.latest().clock)
        assert published is not None
        assert published.reasoning == "precooling ahead of peak"
        assert published.zone("CORE_ZN").heating_setpoint_c == 19.0

    def test_rejects_zone_names_not_in_the_zone_map(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_actuate.propose_policy(
            state,
            [
                ZoneSetpointProposal(
                    zone="CORE_ZN", heating_setpoint_c=21.0, cooling_setpoint_c=24.0
                ),
                ZoneSetpointProposal(
                    zone="NOT_A_REAL_ZONE", heating_setpoint_c=21.0, cooling_setpoint_c=24.0
                ),
            ],
            reasoning="test",
        )
        assert result.accepted is True
        assert result.rejected_zones == ("NOT_A_REAL_ZONE",)

    def test_all_zones_invalid_is_refused_outright(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_actuate.propose_policy(
            state,
            [
                ZoneSetpointProposal(
                    zone="GHOST_ZONE", heating_setpoint_c=21.0, cooling_setpoint_c=24.0
                )
            ],
            reasoning="test",
        )
        assert result.accepted is False
        assert result.rejected_zones == ("GHOST_ZONE",)

    def test_custom_ttl_is_honoured(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        tools_actuate.propose_policy(
            state,
            [
                ZoneSetpointProposal(
                    zone="CORE_ZN", heating_setpoint_c=21.0, cooling_setpoint_c=24.0
                )
            ],
            reasoning="test",
            ttl_minutes=15.0,
        )
        published = state.policy.current(state.telemetry.latest().clock)
        assert published is not None
        assert published.ttl_minutes == 15.0


class TestRequestZoneSetpoint:
    def test_rejects_when_no_simulation_is_active(self) -> None:
        state = make_state()
        result = tools_actuate.request_zone_setpoint(state, zone="CORE_ZN", reasoning="test")
        assert result.accepted is False

    def test_unknown_zone_is_rejected(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        result = tools_actuate.request_zone_setpoint(state, zone="GHOST_ZONE", reasoning="test")
        assert result.accepted is False
        assert result.rejected_zones == ("GHOST_ZONE",)

    def test_omitted_setpoint_carries_the_current_value_through(self) -> None:
        state = make_state()
        state.telemetry.put_nowait(make_sample(zones=(make_zone("CORE_ZN"),)))
        tools_actuate.request_zone_setpoint(
            state, zone="CORE_ZN", reasoning="raise cooling only", cooling_c=27.0
        )
        published = state.policy.current(state.telemetry.latest().clock)
        assert published is not None
        setpoint = published.zone("CORE_ZN")
        assert setpoint is not None
        assert setpoint.heating_setpoint_c == 21.0  # unchanged, from make_zone's default
        assert setpoint.cooling_setpoint_c == 27.0
