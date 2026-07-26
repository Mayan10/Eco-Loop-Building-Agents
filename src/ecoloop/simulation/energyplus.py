"""The real :class:`~ecoloop.simulation.backend.SimulationBackend` adapter.

Every ``pyenergyplus`` call needs the same opaque ``state`` handle, and every
callback EnergyPlus invokes is handed that same ``state`` as its only
argument. This module is where that gets threaded through so nothing above it
— :mod:`ecoloop.simulation.handles`, the reflex callback, all of ``control/``
— ever has to see a ``c_void_p``.

One :class:`EnergyPlusBackend` instance is good for exactly one run. EnergyPlus
state is not reusable across runs in the same process (AGENTS.md landmine
#5): :meth:`EnergyPlusBackend.run` deletes its state in a ``finally`` block, so
a caller running several controllers back to back must construct a fresh
backend per run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ecoloop.errors import SimulationFatalError
from ecoloop.logging import get_logger
from ecoloop.simulation.locate import EnergyPlusInstall, import_energyplus_api

__all__ = ["EnergyPlusBackend"]

_logger = get_logger(__name__, component="simulation")

#: Value returned by ``kind_of_sim()`` during the weather-file run period,
#: determined empirically rather than documented — see AGENTS.md landmine.
_KIND_OF_SIM_WEATHER_RUN_PERIOD = 3


def _invoke_guarded(callback: Callable[[], None], *, point: str) -> None:
    """Call a registered callback, never letting an exception cross into C++.

    An exception raised inside an EnergyPlus callback unwinds into the C++
    runtime and hard-crashes the process with no traceback (AGENTS.md
    invariant #1). This is the one place in the codebase that boundary is
    enforced, so every callback registered through this backend — reflex,
    diagnostics, whatever comes later — is safe by construction rather than by
    each caller remembering to wrap itself.

    Args:
        callback: The zero-argument callback to invoke.
        point: Which registration point this is, for log context.
    """
    try:
        callback()
    except Exception:
        _logger.exception(
            "callback raised; suppressing to protect the EnergyPlus process",
            callback_point=point,
        )


class EnergyPlusBackend:
    """Adapts the real ``pyenergyplus`` API to :class:`SimulationBackend`."""

    def __init__(self, install: EnergyPlusInstall) -> None:
        """Create a backend and a fresh EnergyPlus state for it.

        Args:
            install: A resolved EnergyPlus installation, from
                :func:`ecoloop.simulation.locate.discover_energyplus`.
        """
        self._api: Any = import_energyplus_api(install)
        self._state = self._api.state_manager.new_state()
        self._deleted = False

    def run(self, *, idf_path: Path, weather_path: Path, output_dir: Path) -> int:
        """Run EnergyPlus to completion against a prepared IDF.

        Args:
            idf_path: Prepared IDF path.
            weather_path: EPW weather file path.
            output_dir: Directory to write EnergyPlus outputs into.

        Returns:
            The engine's process exit code.

        Raises:
            SimulationFatalError: If the state was already deleted (this
                backend was already used for a run).
        """
        if self._deleted:
            raise SimulationFatalError(
                "EnergyPlusBackend reused after its state was deleted; "
                "construct a fresh backend per run"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        # EnergyPlus prints extensively to the process's real stdout, which is
        # reserved for the MCP stdio transport (AGENTS.md, logging.py). This is
        # not a tunable — it is always suppressed.
        self._api.runtime.set_console_output_status(self._state, False)
        args = ["-w", str(weather_path), "-d", str(output_dir), str(idf_path)]
        try:
            return int(self._api.runtime.run_energyplus(self._state, args))
        finally:
            self._api.state_manager.delete_state(self._state)
            self._deleted = True

    def request_variable(self, variable: str, key: str) -> None:
        """See :meth:`SimulationBackend.request_variable`."""
        self._api.exchange.request_variable(self._state, variable, key)

    def get_variable_handle(self, variable: str, key: str) -> int:
        """See :meth:`SimulationBackend.get_variable_handle`."""
        return int(self._api.exchange.get_variable_handle(self._state, variable, key))

    def get_variable_value(self, handle: int) -> float:
        """See :meth:`SimulationBackend.get_variable_value`."""
        return float(self._api.exchange.get_variable_value(self._state, handle))

    def get_meter_handle(self, meter: str) -> int:
        """See :meth:`SimulationBackend.get_meter_handle`."""
        return int(self._api.exchange.get_meter_handle(self._state, meter))

    def get_meter_value(self, handle: int) -> float:
        """See :meth:`SimulationBackend.get_meter_value`."""
        return float(self._api.exchange.get_meter_value(self._state, handle))

    def get_actuator_handle(self, component_type: str, control_type: str, key: str) -> int:
        """See :meth:`SimulationBackend.get_actuator_handle`."""
        return int(
            self._api.exchange.get_actuator_handle(self._state, component_type, control_type, key)
        )

    def set_actuator_value(self, handle: int, value: float) -> None:
        """See :meth:`SimulationBackend.set_actuator_value`."""
        self._api.exchange.set_actuator_value(self._state, handle, value)

    def api_data_fully_ready(self) -> bool:
        """See :meth:`SimulationBackend.api_data_fully_ready`."""
        return bool(self._api.exchange.api_data_fully_ready(self._state))

    def warmup_flag(self) -> bool:
        """See :meth:`SimulationBackend.warmup_flag`."""
        return bool(self._api.exchange.warmup_flag(self._state))

    def is_weather_run_period(self) -> bool:
        """See :meth:`SimulationBackend.is_weather_run_period`.

        ``kind_of_sim()`` is documented by ``pyenergyplus`` only as "an
        integer value of current environment" with no key. ``3`` was
        determined empirically (AGENTS.md landmine): it is the value observed
        throughout the real weather-file run period, while every sizing
        design-day environment reports ``1``.
        """
        return int(self._api.exchange.kind_of_sim(self._state)) == _KIND_OF_SIM_WEATHER_RUN_PERIOD

    def current_environment_name(self) -> str:
        """See :meth:`SimulationBackend.current_environment_name`.

        The Python data-exchange API exposes only a numeric environment index,
        not a name, so this returns a synthetic label built from that index.
        It is stable and unique per environment, which is all environment-
        transition detection (AGENTS.md landmine #7) requires.
        """
        num = self._api.exchange.current_environment_num(self._state)
        return f"environment-{num}"

    def clock(self) -> tuple[int, int, int, int, int, int]:
        """See :meth:`SimulationBackend.clock`.

        EnergyPlus reports ``hour`` on a ``1-24`` scale; this normalises to
        ``0-23`` so :class:`ecoloop.bus.models.SimClock` never has to know
        about the EnergyPlus convention. ``minutes()`` is not reliably
        ``0-59`` either — it has been observed to return values past 60 near
        environment boundaries — so the whole ``(hour, minute)`` pair is
        rolled through :class:`datetime.timedelta` rather than trusted
        field-by-field, which correctly carries any overflow into the day,
        month, or year as needed instead of raising deep inside
        :class:`~ecoloop.bus.models.SimClock`'s validation.
        """
        exchange = self._api.exchange
        state = self._state
        base = datetime(int(exchange.year(state)), int(exchange.month(state)), 1) + timedelta(
            days=int(exchange.day_of_month(state)) - 1,
            hours=int(exchange.hour(state)) - 1,
            minutes=int(exchange.minutes(state)),
        )
        return (
            base.year,
            base.month,
            base.day,
            base.hour,
            base.minute,
            int(exchange.day_of_week(state)),
        )

    def on_begin_new_environment(self, callback: Callable[[], None]) -> None:
        """See :meth:`SimulationBackend.on_begin_new_environment`."""

        def _trampoline(_state: object) -> None:
            _invoke_guarded(callback, point="begin_new_environment")

        self._api.runtime.callback_begin_new_environment(self._state, _trampoline)

    def on_end_zone_timestep(self, callback: Callable[[], None]) -> None:
        """See :meth:`SimulationBackend.on_end_zone_timestep`."""

        def _trampoline(_state: object) -> None:
            _invoke_guarded(callback, point="end_zone_timestep")

        self._api.runtime.callback_end_zone_timestep_after_zone_reporting(self._state, _trampoline)
