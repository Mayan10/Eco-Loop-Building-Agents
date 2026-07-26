"""Named energy-conservation measures the rule-based controller composes.

Each function here is one deterministic, independently testable strategy —
occupancy setback, economiser-driven cooling relaxation — expressed as a pure
transform on a proposed setpoint pair. :mod:`ecoloop.control.rulebased`
applies them in sequence; nothing here talks to EnergyPlus, a policy store,
or any other layer, so each measure can be reasoned about (and tested)
completely on its own.

**Scope note.** ``config/default.yaml``'s ``control.rulebased`` section also
declares ``supply_air_reset_enabled`` and ``precool_enabled``. Neither has a
measure here yet:

* Supply-air reset needs an air-handling-unit-level actuator (a supply node's
  temperature setpoint), not a zone thermostat — this project only wires zone
  and lighting actuators (``config/zones.yaml``) so far.
* Precooling needs a forward look at each zone's occupancy *schedule* to know
  when the next occupied period begins; :class:`~ecoloop.bus.models.ZoneTelemetry`
  only carries the current instantaneous ``occupancy_fraction``, not a
  schedule to look ahead in.

Both are real, tracked gaps rather than oversights — adding either is a
config-plus-actuator change, not a rewrite of this module.
"""

from __future__ import annotations

from ecoloop.config import RuleBasedSettings

__all__ = ["economiser_shift", "unoccupied_setback"]


def unoccupied_setback(
    heating_c: float,
    cooling_c: float,
    *,
    occupied: bool,
    settings: RuleBasedSettings,
) -> tuple[float, float]:
    """Widen the deadband around an occupied baseline while a zone is empty.

    A classic ASHRAE Guideline 36 measure: there is no comfort requirement to
    satisfy with nobody in the zone, so heating is relaxed downward and
    cooling relaxed upward by half the configured widening each, symmetric
    around the occupied setpoints passed in.

    Args:
        heating_c: The occupied-baseline heating setpoint.
        cooling_c: The occupied-baseline cooling setpoint.
        occupied: Whether the zone is currently occupied.
        settings: Rule-based controller tunables.

    Returns:
        The (possibly widened) ``(heating_c, cooling_c)`` pair, unchanged if
        the zone is occupied or the measure is disabled.
    """
    if not settings.setback_enabled or occupied:
        return heating_c, cooling_c
    half_widening = settings.deadband_widening_unoccupied_c / 2.0
    return heating_c - half_widening, cooling_c + half_widening


def economiser_shift(
    heating_c: float,
    cooling_c: float,
    *,
    outdoor_air_temperature_c: float,
    settings: RuleBasedSettings,
) -> tuple[float, float]:
    """Lower the cooling setpoint when outdoor air is in the free-cooling band.

    **Honesty note.** A textbook air-side economiser gets its savings from an
    outdoor-air damper actuator: when outdoor conditions are favourable, it
    admits more outdoor air instead of running the compressor harder. This
    project has no such actuator wired (``config/zones.yaml`` exposes only
    zone thermostats and the lighting schedule) — this measure can only ask
    the existing DX cooling coil to reach a colder setpoint, which still costs
    compressor energy regardless of how mild the outdoor air is. It is a
    modest, defensible heuristic (favourable outdoor conditions correlate
    with lower cooling *load*, so the extra setpoint aggressiveness costs
    less than it would on a hot day) rather than genuine free cooling, and
    should not be read as claiming the latter. Measured against the fast
    profile's July window, it fired on well under 1% of timesteps — Chicago
    summer rarely sits in a 4-18°C band during occupied hours — so its energy
    effect is minor here and would only become material in shoulder-season
    runs.

    Args:
        heating_c: The setpoint pair's current heating value (passed through
            unchanged).
        cooling_c: The current cooling setpoint, before this measure.
        outdoor_air_temperature_c: Current outdoor dry-bulb temperature.
        settings: Rule-based controller tunables.

    Returns:
        The ``(heating_c, cooling_c)`` pair, with cooling shifted down when
        the economiser band applies and the measure is enabled.
    """
    if not settings.economiser_enabled:
        return heating_c, cooling_c
    in_band = (
        settings.economiser_min_oat_c <= outdoor_air_temperature_c <= settings.economiser_max_oat_c
    )
    if not in_band:
        return heating_c, cooling_c
    return heating_c, cooling_c - settings.economiser_setpoint_shift_c
