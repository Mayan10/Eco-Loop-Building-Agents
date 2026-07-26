"""Wire the reflex tier's telemetry publication and actuation into a run.

:class:`ReflexCallbacks` is the only thing in the simulation layer that
combines a live :class:`~ecoloop.simulation.backend.SimulationBackend`, a
resolved :class:`~ecoloop.simulation.handles.HandleRegistry`, and — the two
things everything above this layer must reach through it — a place to
publish samples and, optionally, a :class:`~ecoloop.control.reflex.ReflexController`
to decide what to actuate. It knows nothing about ``TelemetryBus`` itself —
the publish sink is an injected callable — so this module has no dependency
on ``bus`` beyond the value types in :mod:`ecoloop.bus.models`, which is
exactly what the layering rule in AGENTS.md §5 requires. Depending on
``control.reflex`` is fine: nothing forbids simulation importing control, and
this is precisely the seam where "what to actuate" (control's job) meets
"how to actuate it" (a handle and a backend call, simulation's job).

Exceptions raised by the callables registered here never reach EnergyPlus:
the backend's own trampoline (``EnergyPlusBackend._invoke_guarded``) is the one
place that boundary is enforced, so this module can write straightforward
logic without repeating that guard at every call site. Actuation is wrapped
in its own try/except regardless, so a bug newly introduced in the control
stack degrades to "this timestep's setpoints are left alone" rather than
also silently losing that timestep's telemetry.
"""

from __future__ import annotations

from collections.abc import Callable

from ecoloop.bus.models import SimClock, TelemetrySample
from ecoloop.control.reflex import ReflexController
from ecoloop.logging import get_logger
from ecoloop.simulation.backend import SimulationBackend
from ecoloop.simulation.handles import HandleRegistry, ZoneMap

__all__ = ["ReflexCallbacks"]

_logger = get_logger(__name__, component="reflex")


class ReflexCallbacks:
    """Registers the begin-environment and end-timestep hooks for a run.

    Args:
        backend: The live simulation backend to read from and register against.
        zone_map: The point map describing which zones to sample.
        handles: A :class:`HandleRegistry` already primed with
            :meth:`~ecoloop.simulation.handles.HandleRegistry.request_all`
            (this class calls ``ensure_resolved`` itself, once readiness is
            reported).
        on_sample: Called with each completed, non-warmup
            :class:`~ecoloop.bus.models.TelemetrySample`. In production this is
            ``TelemetryBus.put_nowait``; tests may pass ``list.append``.
        reflex_controller: Optional control-layer decision maker. When given,
            its decision is clamped and actuated after telemetry publishes for
            the same timestep. When omitted, this run only observes — no
            actuation happens and the IDF's native schedule is left in effect.
    """

    def __init__(
        self,
        backend: SimulationBackend,
        zone_map: ZoneMap,
        handles: HandleRegistry,
        on_sample: Callable[[TelemetrySample], None],
        reflex_controller: ReflexController | None = None,
    ) -> None:
        """Bind this callback set to a backend, zone map, registry and sink."""
        self._backend = backend
        self._zone_map = zone_map
        self._handles = handles
        self._on_sample = on_sample
        self._reflex_controller = reflex_controller
        self._timestep_index = 0

    def register(self) -> None:
        """Register both callbacks with the backend.

        Must be called before the backend's :meth:`~SimulationBackend.run`.
        """
        self._backend.on_begin_new_environment(self._on_begin_new_environment)
        self._backend.on_end_zone_timestep(self._on_end_zone_timestep)

    def _on_begin_new_environment(self) -> None:
        """Reset per-environment accumulators.

        Fires once per environment — sizing periods, then the run period
        (AGENTS.md landmine #7). Blending a counter across that boundary would
        silently corrupt it, so the timestep index restarts at zero here
        rather than only at process start.
        """
        environment = self._backend.current_environment_name()
        self._timestep_index = 0
        _logger.info("new environment started", environment=environment)

    def _on_end_zone_timestep(self) -> None:
        """Publish one telemetry sample, if the run is ready to be sampled.

        No-ops during warmup, before handles resolve, and outside the
        weather-file run period — described in AGENTS.md invariant #6,
        landmine #3, and the newest landmine on sizing-period timesteps,
        respectively. None of these are error conditions: warmup happens at
        the start of every environment, handle resolution completing on, say,
        the third timestep rather than the first is expected engine
        behaviour, and sizing design-days run before the run period does.
        """
        self._handles.ensure_resolved(self._backend)
        if not self._handles.resolved:
            return
        if self._backend.warmup_flag():
            return
        if not self._backend.is_weather_run_period():
            return

        self._timestep_index += 1
        environment = self._backend.current_environment_name()
        year, month, day, hour, minute, day_of_week = self._backend.clock()

        sample = TelemetrySample(
            clock=SimClock(
                year=year,
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                day_of_week=day_of_week,
            ),
            timestep_index=self._timestep_index,
            environment=environment,
            warmup=False,
            site=self._handles.read_site_conditions(self._backend),
            zones=tuple(
                self._handles.read_zone_telemetry(self._backend, zone.name)
                for zone in self._zone_map.zones
            ),
            meters=self._handles.read_meters(self._backend),
        )
        self._on_sample(sample)

        if self._reflex_controller is not None:
            self._actuate(sample)

    def _actuate(self, sample: TelemetrySample) -> None:
        """Decide and apply setpoints for this timestep, isolated from telemetry.

        Wrapped separately from telemetry construction above so a bug in the
        control stack degrades to "this timestep's setpoints are left alone"
        rather than also losing that timestep's sample — the outer callback
        guard would otherwise treat the two as one all-or-nothing unit.

        Args:
            sample: The sample just published for this timestep.
        """
        assert self._reflex_controller is not None  # noqa: S101 - guarded by the caller
        try:
            decisions = self._reflex_controller.decide(sample)
        except Exception:
            _logger.exception("reflex controller raised; leaving setpoints unchanged")
            return

        for zone_name, result in decisions.items():
            try:
                heating_handle = self._handles.actuator_handle(zone_name, "heating_setpoint_c")
                cooling_handle = self._handles.actuator_handle(zone_name, "cooling_setpoint_c")
                self._backend.set_actuator_value(heating_handle, result.heating_setpoint_c)
                self._backend.set_actuator_value(cooling_handle, result.cooling_setpoint_c)
            except Exception:
                _logger.exception("failed to actuate zone; leaving it unchanged", zone=zone_name)
