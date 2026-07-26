"""Tests for the guardrail clamp chain.

Includes property tests: the envelope and deadband invariants must hold for
*arbitrary* proposed setpoints, not just the handful of examples a controller
happens to produce today. That is the whole promise behind AGENTS.md
invariant #2 - "a compromised or hallucinating model cannot drive a zone
outside the envelope" has to be true for every input, not just typical ones.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from ecoloop.config import GuardrailSettings
from ecoloop.control.guardrails import (
    ZoneActuationMemory,
    check_zone_temp_alarm,
    clamp_lighting_fraction,
    clamp_setpoints,
)

DEFAULT_GUARDRAILS = GuardrailSettings(
    heating_setpoint_min_c=15.0,
    heating_setpoint_max_c=23.0,
    cooling_setpoint_min_c=21.0,
    cooling_setpoint_max_c=30.0,
    min_deadband_c=2.0,
    max_setpoint_change_per_hour_c=1.5,
    min_hold_minutes=30.0,
    zone_temp_alarm_min_c=12.0,
    zone_temp_alarm_max_c=32.0,
    min_lighting_fraction_occupied=0.6,
)


class TestEnvelopeClamping:
    def test_proposal_within_envelope_passes_through(self) -> None:
        result = clamp_setpoints(
            proposed_heating_c=20.0,
            proposed_cooling_c=25.0,
            memory=ZoneActuationMemory(),
            elapsed_minutes=0.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert result.heating_setpoint_c == 20.0
        assert result.cooling_setpoint_c == 25.0
        assert not result.was_clamped

    def test_heating_above_max_is_clamped(self) -> None:
        result = clamp_setpoints(
            proposed_heating_c=99.0,
            proposed_cooling_c=99.0,
            memory=ZoneActuationMemory(),
            elapsed_minutes=0.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert result.heating_setpoint_c == DEFAULT_GUARDRAILS.heating_setpoint_max_c
        assert result.was_clamped

    def test_cooling_below_min_is_clamped(self) -> None:
        result = clamp_setpoints(
            proposed_heating_c=15.0,
            proposed_cooling_c=-10.0,
            memory=ZoneActuationMemory(),
            elapsed_minutes=0.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert result.cooling_setpoint_c >= DEFAULT_GUARDRAILS.cooling_setpoint_min_c


class TestDeadband:
    def test_inverted_setpoints_are_widened(self) -> None:
        """Heating >= cooling causes simultaneous heating and cooling."""
        result = clamp_setpoints(
            proposed_heating_c=22.0,
            proposed_cooling_c=21.5,
            memory=ZoneActuationMemory(),
            elapsed_minutes=0.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert (
            result.cooling_setpoint_c - result.heating_setpoint_c
            >= DEFAULT_GUARDRAILS.min_deadband_c
        )

    def test_widening_that_would_exceed_cooling_max_lowers_heating_instead(self) -> None:
        """Under DEFAULT_GUARDRAILS, heating's own envelope max (23) sits well
        below cooling_max - deadband (28), so raising cooling can always
        restore the deadband and the "lower heating instead" branch never
        triggers - this uses a guardrail config where the two ranges overlap
        more tightly, so raising cooling to heating_c + deadband would
        overshoot cooling_setpoint_max_c and heating must come down instead.
        """
        tight_guardrails = DEFAULT_GUARDRAILS.model_copy(
            update={"heating_setpoint_min_c": 15.0, "heating_setpoint_max_c": 29.0}
        )
        result = clamp_setpoints(
            proposed_heating_c=29.0,  # heating at its max
            proposed_cooling_c=29.5,  # gap only 0.5, below the 2.0 deadband
            memory=ZoneActuationMemory(),
            elapsed_minutes=0.0,
            guardrails=tight_guardrails,
        )
        assert result.cooling_setpoint_c == tight_guardrails.cooling_setpoint_max_c
        assert result.heating_setpoint_c == pytest.approx(
            tight_guardrails.cooling_setpoint_max_c - tight_guardrails.min_deadband_c
        )
        assert (
            result.cooling_setpoint_c - result.heating_setpoint_c >= tight_guardrails.min_deadband_c
        )


class TestRateLimitAndHold:
    def test_first_ever_proposal_is_not_rate_limited(self) -> None:
        result = clamp_setpoints(
            proposed_heating_c=15.0,
            proposed_cooling_c=30.0,
            memory=ZoneActuationMemory(),
            elapsed_minutes=0.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert result.heating_setpoint_c == 15.0
        assert result.cooling_setpoint_c == 30.0

    def test_large_jump_is_rate_limited_relative_to_previous(self) -> None:
        memory = ZoneActuationMemory(
            last_heating_setpoint_c=20.0, last_cooling_setpoint_c=24.0, minutes_since_change=60.0
        )
        result = clamp_setpoints(
            proposed_heating_c=23.0,  # +3C in one hour, cap is 1.5C/hour
            proposed_cooling_c=24.0,
            memory=memory,
            elapsed_minutes=60.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert result.heating_setpoint_c == 21.5
        assert result.was_clamped

    def test_change_within_min_hold_time_is_refused(self) -> None:
        memory = ZoneActuationMemory(
            last_heating_setpoint_c=20.0, last_cooling_setpoint_c=24.0, minutes_since_change=5.0
        )
        result = clamp_setpoints(
            proposed_heating_c=20.5,
            proposed_cooling_c=24.5,
            memory=memory,
            elapsed_minutes=5.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert result.heating_setpoint_c == 20.0
        assert result.cooling_setpoint_c == 24.0

    def test_unchanged_proposal_is_never_held_or_limited(self) -> None:
        memory = ZoneActuationMemory(
            last_heating_setpoint_c=20.0, last_cooling_setpoint_c=24.0, minutes_since_change=0.0
        )
        result = clamp_setpoints(
            proposed_heating_c=20.0,
            proposed_cooling_c=24.0,
            memory=memory,
            elapsed_minutes=0.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert not result.was_clamped


class TestDeadbandSurvivesRateLimiting:
    def test_deadband_holds_even_from_a_corrupted_previous_pair(self) -> None:
        """Defence in depth: rate-limiting caps heating and cooling
        independently, each relative to its own previous value. If a bug
        elsewhere ever left ZoneActuationMemory holding a previous pair that
        does not itself satisfy the deadband, independent per-field capping
        could otherwise produce a result that doesn't either. The final
        deadband re-check must catch that regardless.
        """
        memory = ZoneActuationMemory(
            last_heating_setpoint_c=22.0,  # invalid previous state: gap is 0.5,
            last_cooling_setpoint_c=22.5,  # well under min_deadband_c (2.0)
            minutes_since_change=60.0,
        )
        # This proposal already satisfies the deadband on its own (gap 6.0),
        # so the *first* deadband pass is a no-op. Rate-limiting then pulls
        # heating down toward the corrupted previous value more than cooling
        # does (cooling's proposed delta happens to sit exactly at the cap),
        # producing an intermediate pair with gap 0.5 - which only the
        # second deadband pass catches.
        result = clamp_setpoints(
            proposed_heating_c=15.0,
            proposed_cooling_c=21.0,
            memory=memory,
            elapsed_minutes=60.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        assert (
            result.cooling_setpoint_c - result.heating_setpoint_c
            >= DEFAULT_GUARDRAILS.min_deadband_c
        )


class TestZoneActuationMemory:
    def test_record_resets_minutes_since_change_on_a_real_change(self) -> None:
        memory = ZoneActuationMemory(
            last_heating_setpoint_c=20.0, last_cooling_setpoint_c=24.0, minutes_since_change=45.0
        )
        result = clamp_setpoints(
            proposed_heating_c=20.5,
            proposed_cooling_c=24.5,
            memory=memory,
            elapsed_minutes=10.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        memory.record(result, elapsed_minutes=10.0)
        assert memory.minutes_since_change == 0.0

    def test_record_accumulates_minutes_when_unchanged(self) -> None:
        memory = ZoneActuationMemory(
            last_heating_setpoint_c=20.0, last_cooling_setpoint_c=24.0, minutes_since_change=10.0
        )
        result = clamp_setpoints(
            proposed_heating_c=20.0,
            proposed_cooling_c=24.0,
            memory=memory,
            elapsed_minutes=10.0,
            guardrails=DEFAULT_GUARDRAILS,
        )
        memory.record(result, elapsed_minutes=10.0)
        assert memory.minutes_since_change == 20.0


class TestZoneTempAlarm:
    def test_within_bounds_has_no_alarm(self) -> None:
        assert check_zone_temp_alarm(22.0, DEFAULT_GUARDRAILS) is None

    def test_below_floor_alarms(self) -> None:
        assert check_zone_temp_alarm(5.0, DEFAULT_GUARDRAILS) is not None

    def test_above_ceiling_alarms(self) -> None:
        assert check_zone_temp_alarm(40.0, DEFAULT_GUARDRAILS) is not None


class TestLightingFraction:
    def test_occupied_zone_is_floored(self) -> None:
        assert (
            clamp_lighting_fraction(0.1, occupied=True, guardrails=DEFAULT_GUARDRAILS)
            == DEFAULT_GUARDRAILS.min_lighting_fraction_occupied
        )

    def test_unoccupied_zone_may_go_to_zero(self) -> None:
        assert clamp_lighting_fraction(0.0, occupied=False, guardrails=DEFAULT_GUARDRAILS) == 0.0

    def test_fraction_is_clamped_to_unit_range(self) -> None:
        assert clamp_lighting_fraction(1.5, occupied=False, guardrails=DEFAULT_GUARDRAILS) == 1.0
        assert clamp_lighting_fraction(-1.0, occupied=False, guardrails=DEFAULT_GUARDRAILS) == 0.0


# --------------------------------------------------------------------------- #
# Property tests: the envelope and deadband invariants must hold for any
# proposal, any actuation history, and any elapsed time - not just the
# examples above.
# --------------------------------------------------------------------------- #
_reasonable_temp = st.floats(min_value=-50.0, max_value=80.0, allow_nan=False, allow_infinity=False)
_reasonable_minutes = st.floats(
    min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False
)


@given(
    proposed_heating_c=_reasonable_temp,
    proposed_cooling_c=_reasonable_temp,
    previous_heating_c=_reasonable_temp,
    previous_cooling_c=_reasonable_temp,
    minutes_since_change=_reasonable_minutes,
)
def test_clamp_result_never_leaves_the_envelope(
    proposed_heating_c: float,
    proposed_cooling_c: float,
    previous_heating_c: float,
    previous_cooling_c: float,
    minutes_since_change: float,
) -> None:
    memory = ZoneActuationMemory(
        last_heating_setpoint_c=previous_heating_c,
        last_cooling_setpoint_c=previous_cooling_c,
        minutes_since_change=minutes_since_change,
    )
    result = clamp_setpoints(
        proposed_heating_c=proposed_heating_c,
        proposed_cooling_c=proposed_cooling_c,
        memory=memory,
        elapsed_minutes=minutes_since_change,
        guardrails=DEFAULT_GUARDRAILS,
    )
    assert (
        DEFAULT_GUARDRAILS.heating_setpoint_min_c
        <= result.heating_setpoint_c
        <= DEFAULT_GUARDRAILS.heating_setpoint_max_c
    )
    assert (
        DEFAULT_GUARDRAILS.cooling_setpoint_min_c
        <= result.cooling_setpoint_c
        <= DEFAULT_GUARDRAILS.cooling_setpoint_max_c
    )


@given(
    proposed_heating_c=_reasonable_temp,
    proposed_cooling_c=_reasonable_temp,
)
def test_clamp_result_never_violates_the_deadband_with_no_history(
    proposed_heating_c: float,
    proposed_cooling_c: float,
) -> None:
    """No actuation history means no rate/hold interference - isolates the
    envelope+deadband stages, which must satisfy the deadband unconditionally."""
    result = clamp_setpoints(
        proposed_heating_c=proposed_heating_c,
        proposed_cooling_c=proposed_cooling_c,
        memory=ZoneActuationMemory(),
        elapsed_minutes=0.0,
        guardrails=DEFAULT_GUARDRAILS,
    )
    assert (
        result.cooling_setpoint_c - result.heating_setpoint_c
        >= DEFAULT_GUARDRAILS.min_deadband_c - 1e-9
    )


@given(
    proposed_heating_c=_reasonable_temp,
    proposed_cooling_c=_reasonable_temp,
    previous_cooling_c=st.floats(min_value=21.0, max_value=30.0, allow_nan=False),
    previous_gap=st.floats(min_value=2.0, max_value=8.0, allow_nan=False),
)
def test_rate_limit_is_never_exceeded_by_more_than_a_deadband_correction(
    proposed_heating_c: float,
    proposed_cooling_c: float,
    previous_cooling_c: float,
    previous_gap: float,
) -> None:
    """Over a one-hour gap, the applied change must never exceed the configured
    per-hour cap by more than the final deadband safety net can add.

    The rate limit is not an absolute ceiling: if capping heating and cooling
    independently would leave them violating the deadband (AGENTS.md - heating
    at or above cooling causes simultaneous heating and cooling), the final
    deadband pass widens one of them further, deliberately prioritising "never
    heat and cool at once" over the rate cap. That widening is bounded by
    min_deadband_c, so the total deviation from the cap is bounded too.

    ``previous_heating_c`` is derived from ``previous_cooling_c`` minus a gap
    of at least ``min_deadband_c``, and discarded via ``assume`` whenever that
    lands outside heating's own envelope — this keeps the generated
    "previous" pair realistic: both values within their own envelope *and*
    satisfying the deadband, exactly what a prior valid clamp would have
    produced. An already-corrupted previous pair is a distinct scenario,
    covered separately by ``test_deadband_holds_even_from_a_corrupted_previous_pair``,
    and can require a larger correction than this bound assumes.
    """
    previous_heating_c = previous_cooling_c - previous_gap
    assume(
        DEFAULT_GUARDRAILS.heating_setpoint_min_c
        <= previous_heating_c
        <= DEFAULT_GUARDRAILS.heating_setpoint_max_c
    )
    memory = ZoneActuationMemory(
        last_heating_setpoint_c=previous_heating_c,
        last_cooling_setpoint_c=previous_cooling_c,
        minutes_since_change=60.0,
    )
    result = clamp_setpoints(
        proposed_heating_c=proposed_heating_c,
        proposed_cooling_c=proposed_cooling_c,
        memory=memory,
        elapsed_minutes=60.0,
        guardrails=DEFAULT_GUARDRAILS,
    )
    max_delta = (
        DEFAULT_GUARDRAILS.max_setpoint_change_per_hour_c + DEFAULT_GUARDRAILS.min_deadband_c + 1e-9
    )
    assert abs(result.heating_setpoint_c - previous_heating_c) <= max_delta
    assert abs(result.cooling_setpoint_c - previous_cooling_c) <= max_delta
