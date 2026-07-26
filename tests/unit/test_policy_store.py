"""Tests for ControlPolicy and PolicyStore."""

from __future__ import annotations

from ecoloop.bus.models import SimClock
from ecoloop.bus.policy import ControlPolicy, PolicySource, PolicyStore, ZoneSetpoint


def clock(minute: int, *, hour: int = 0) -> SimClock:
    return SimClock(year=1999, month=1, day=1, hour=hour, minute=minute, day_of_week=1)


def policy(*, issued_at: SimClock, ttl_minutes: float = 90.0) -> ControlPolicy:
    return ControlPolicy(
        issued_at=issued_at,
        source=PolicySource.AGENT,
        ttl_minutes=ttl_minutes,
        zone_setpoints=(
            ZoneSetpoint(zone="CORE_ZN", heating_setpoint_c=21.0, cooling_setpoint_c=24.0),
        ),
    )


class TestControlPolicy:
    def test_zone_lookup_is_case_insensitive(self) -> None:
        p = policy(issued_at=clock(0))
        assert p.zone("core_zn") is not None
        assert p.zone("CORE_ZN") is not None

    def test_zone_lookup_missing_zone_is_none(self) -> None:
        p = policy(issued_at=clock(0))
        assert p.zone("PERIMETER_ZN_1") is None

    def test_age_minutes_computes_elapsed_simulation_time(self) -> None:
        p = policy(issued_at=clock(0))
        assert p.age_minutes(clock(30)) == 30.0

    def test_age_minutes_spans_hour_boundary(self) -> None:
        p = policy(issued_at=clock(50, hour=0))
        assert p.age_minutes(clock(10, hour=1)) == 20.0

    def test_policy_id_is_generated_when_omitted(self) -> None:
        first = policy(issued_at=clock(0))
        second = policy(issued_at=clock(0))
        assert first.policy_id != second.policy_id


class TestPolicyStore:
    def test_empty_store_returns_none(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        assert store.current(clock(0)) is None

    def test_published_policy_is_returned_while_fresh(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        store.publish(policy(issued_at=clock(0), ttl_minutes=90.0))
        assert store.current(clock(30)) is not None

    def test_policy_expires_after_its_own_ttl(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        store.publish(policy(issued_at=clock(0, hour=0), ttl_minutes=90.0))
        assert store.current(clock(31, hour=1)) is None  # 91 minutes elapsed

    def test_max_age_caps_an_anomalously_long_ttl(self) -> None:
        """A policy declaring a huge TTL cannot outlive the store's hard ceiling."""
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=120.0)
        store.publish(policy(issued_at=clock(0, hour=0), ttl_minutes=10_000.0))
        assert store.current(clock(1, hour=2)) is None  # 121 minutes elapsed

    def test_publish_replaces_the_previous_policy(self) -> None:
        store = PolicyStore(default_ttl_minutes=90.0, max_age_minutes=180.0)
        first = policy(issued_at=clock(0))
        second = policy(issued_at=clock(0))
        store.publish(first)
        store.publish(second)
        assert store.current(clock(0)).policy_id == second.policy_id  # type: ignore[union-attr]
