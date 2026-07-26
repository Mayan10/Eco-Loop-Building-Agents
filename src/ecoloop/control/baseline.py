"""The fixed occupancy-only schedule every other controller must beat.

No weather awareness, no economiser, no rate-of-change reasoning — just "is
anyone in the zone right now": occupied zones get the default setpoints,
empty ones get the wider unoccupied pair. This is deliberately the least
sophisticated controller in the project. Reporting energy savings without
this floor would be reporting savings against nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from ecoloop.bus.models import TelemetrySample
from ecoloop.bus.policy import ControlPolicy, PolicySource, ZoneSetpoint
from ecoloop.config import ControlSettings

__all__ = ["BaselineController"]


class BaselineController:
    """Issues a policy of fixed occupied/unoccupied setpoints, per zone.

    Args:
        zone_names: Zones this controller addresses (typically every
            conditioned zone in ``config/zones.yaml``).
        control: Controller settings, for the four fixed setpoints.
        occupied_threshold_fraction: Fractional occupancy above which a zone
            counts as occupied, from ``comfort.occupied_threshold_fraction``.
        ttl_minutes: How long each issued policy stays valid, from
            ``bus.policy.default_ttl_minutes``.
    """

    def __init__(
        self,
        *,
        zone_names: Sequence[str],
        control: ControlSettings,
        occupied_threshold_fraction: float,
        ttl_minutes: float,
    ) -> None:
        """Bind this controller to the zones and settings it will address."""
        self._zone_names = tuple(zone_names)
        self._control = control
        self._occupied_threshold_fraction = occupied_threshold_fraction
        self._ttl_minutes = ttl_minutes

    def decide(self, sample: TelemetrySample) -> ControlPolicy:
        """Compute the fixed schedule's proposal for every addressed zone.

        Args:
            sample: The latest telemetry sample.

        Returns:
            A policy with one ``ZoneSetpoint`` per zone this controller
            addresses that is present in ``sample``.
        """
        setpoints: list[ZoneSetpoint] = []
        for name in self._zone_names:
            zone = sample.zone(name)
            if zone is None:
                continue
            occupied = zone.is_occupied(self._occupied_threshold_fraction)
            setpoints.append(
                ZoneSetpoint(
                    zone=zone.zone,
                    heating_setpoint_c=(
                        self._control.default_heating_setpoint_c
                        if occupied
                        else self._control.unoccupied_heating_setpoint_c
                    ),
                    cooling_setpoint_c=(
                        self._control.default_cooling_setpoint_c
                        if occupied
                        else self._control.unoccupied_cooling_setpoint_c
                    ),
                )
            )
        return ControlPolicy(
            issued_at=sample.clock,
            source=PolicySource.BASELINE,
            ttl_minutes=self._ttl_minutes,
            zone_setpoints=tuple(setpoints),
            reasoning="fixed occupied/unoccupied schedule, no adaptive logic",
        )
