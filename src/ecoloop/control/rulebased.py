"""The deterministic ASHRAE Guideline-36-flavoured benchmark controller.

This exists to answer the judging question directly: an LLM that only
matches what a simple, well-tuned heuristic already does has added nothing.
:class:`RuleBasedController` composes the named measures in
:mod:`ecoloop.control.ecm` — occupancy setback, then economiser-driven
cooling relaxation — starting from the same occupied baseline
:class:`~ecoloop.control.baseline.BaselineController` uses, so any
difference in outcome between the two is attributable to these measures and
nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

from ecoloop.bus.models import TelemetrySample
from ecoloop.bus.policy import ControlPolicy, PolicySource, ZoneSetpoint
from ecoloop.config import ControlSettings
from ecoloop.control.ecm import economiser_shift, unoccupied_setback

__all__ = ["RuleBasedController"]


class RuleBasedController:
    """Issues a policy from occupancy setback plus economiser adjustment.

    Args:
        zone_names: Zones this controller addresses.
        control: Controller settings, including the ``rulebased`` sub-block.
        occupied_threshold_fraction: Fractional occupancy above which a zone
            counts as occupied, from ``comfort.occupied_threshold_fraction``.
        ttl_minutes: How long each issued policy stays valid.
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
        """Compute this timestep's proposal for every addressed zone.

        Args:
            sample: The latest telemetry sample.

        Returns:
            A policy with one ``ZoneSetpoint`` per zone this controller
            addresses that is present in ``sample``, and a ``reasoning``
            string naming which measures actually fired.
        """
        rulebased = self._control.rulebased
        setpoints: list[ZoneSetpoint] = []
        measures_fired: set[str] = set()

        for name in self._zone_names:
            zone = sample.zone(name)
            if zone is None:
                continue
            occupied = zone.is_occupied(self._occupied_threshold_fraction)

            heating_c = self._control.default_heating_setpoint_c
            cooling_c = self._control.default_cooling_setpoint_c

            widened_heating, widened_cooling = unoccupied_setback(
                heating_c, cooling_c, occupied=occupied, settings=rulebased
            )
            if (widened_heating, widened_cooling) != (heating_c, cooling_c):
                measures_fired.add("unoccupied_setback")
            heating_c, cooling_c = widened_heating, widened_cooling

            shifted_heating, shifted_cooling = economiser_shift(
                heating_c,
                cooling_c,
                outdoor_air_temperature_c=sample.site.outdoor_air_temperature_c,
                settings=rulebased,
            )
            if (shifted_heating, shifted_cooling) != (heating_c, cooling_c):
                measures_fired.add("economiser_shift")
            heating_c, cooling_c = shifted_heating, shifted_cooling

            setpoints.append(
                ZoneSetpoint(
                    zone=zone.zone, heating_setpoint_c=heating_c, cooling_setpoint_c=cooling_c
                )
            )

        reasoning = (
            f"measures applied: {', '.join(sorted(measures_fired))}"
            if measures_fired
            else "no measure applied this timestep; occupied baseline held"
        )
        return ControlPolicy(
            issued_at=sample.clock,
            source=PolicySource.RULEBASED,
            ttl_minutes=self._ttl_minutes,
            zone_setpoints=tuple(setpoints),
            reasoning=reasoning,
        )
