"""The seam between Eco-Loop and an EnergyPlus-shaped simulation engine.

:class:`SimulationBackend` is a :class:`~typing.Protocol`, not a base class.
:mod:`ecoloop.simulation.energyplus` implements it against the real
``pyenergyplus`` API; ⏳ ``tests/fakes/fake_energyplus.py`` implements it against
a lumped-capacitance thermal model. Everything above this layer —
:mod:`ecoloop.simulation.handles`, ⏳ ``callbacks.py``, and all of ``control/`` —
depends only on this protocol, which is what lets the entire control stack run
in CI with no EnergyPlus installed.

The protocol deliberately hides the raw EnergyPlus ``state`` handle. Every
real API call needs it, but callers here never should: a backend instance owns
exactly one state for its lifetime, and hiding it is what keeps
:mod:`ecoloop.simulation.handles` and the reflex callback engine-agnostic.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["SimulationBackend"]

#: EnergyPlus's own sentinel for "this variable, meter, or actuator does not
#: exist". It is returned, never raised — see AGENTS.md landmine #1.
INVALID_HANDLE = -1


@runtime_checkable
class SimulationBackend(Protocol):
    """Everything the simulation layer needs from an EnergyPlus-shaped engine."""

    # -- lifecycle --------------------------------------------------------- #
    def run(self, *, idf_path: Path, weather_path: Path, output_dir: Path) -> int:
        """Run a simulation to completion.

        Args:
            idf_path: Prepared IDF to simulate.
            weather_path: EPW weather file.
            output_dir: Directory EnergyPlus writes its outputs into.

        Returns:
            The engine's process exit code. Zero does not guarantee no
            Severe errors were logged — check the ``.err`` file for that.
        """
        ...

    # -- data exchange: requesting and resolving points --------------------- #
    def request_variable(self, variable: str, key: str) -> None:
        """Declare a variable of interest before the run starts.

        Must be called before :meth:`run`, or the matching
        :meth:`get_variable_handle` call fails for the entire run regardless
        of whether the variable actually exists (AGENTS.md landmine #2).

        Args:
            variable: EnergyPlus output variable name, e.g. ``"Zone Mean Air
                Temperature"``.
            key: The variable's key value, e.g. a zone or People object name.
        """
        ...

    def get_variable_handle(self, variable: str, key: str) -> int:
        """Resolve a previously requested variable to a handle.

        Args:
            variable: Variable name, matching a prior :meth:`request_variable`
                call.
            key: Key value, matching that call.

        Returns:
            A non-negative handle, or :data:`INVALID_HANDLE` if the variable
            does not exist. Never raises.
        """
        ...

    def get_variable_value(self, handle: int) -> float:
        """Read a resolved variable's current value.

        Args:
            handle: A handle previously returned by :meth:`get_variable_handle`.

        Returns:
            The instantaneous value at the current timestep.
        """
        ...

    def get_meter_handle(self, meter: str) -> int:
        """Resolve a meter name to a handle.

        Args:
            meter: Meter name, e.g. ``"ElectricityNet:Facility"``.

        Returns:
            A non-negative handle, or :data:`INVALID_HANDLE` if the meter does
            not exist in this run. Never raises.
        """
        ...

    def get_meter_value(self, handle: int) -> float:
        """Read a meter's accumulated value for the reporting period just ended.

        Args:
            handle: A handle previously returned by :meth:`get_meter_handle`.

        Returns:
            Joules accumulated since the last report. **Always Joules** —
            conversion to kWh happens once, in ``analysis/``.
        """
        ...

    def get_actuator_handle(self, component_type: str, control_type: str, key: str) -> int:
        """Resolve an actuator to a handle.

        Args:
            component_type: EnergyPlus actuator component type, e.g. ``"Zone
                Temperature Control"``.
            control_type: Actuator control type, e.g. ``"Heating Setpoint"``.
            key: Actuator key value, typically a zone name.

        Returns:
            A non-negative handle, or :data:`INVALID_HANDLE` if the actuator
            does not exist. Never raises.
        """
        ...

    def set_actuator_value(self, handle: int, value: float) -> None:
        """Override a resolved actuator for the current timestep.

        Args:
            handle: A handle previously returned by :meth:`get_actuator_handle`.
            value: The value to force.
        """
        ...

    # -- run-state queries --------------------------------------------------- #
    def api_data_fully_ready(self) -> bool:
        """Whether variable and actuator handles may now be resolved.

        Returns:
            ``True`` once EnergyPlus has finished building its internal data
            structures. Resolving handles before this is always
            :data:`INVALID_HANDLE`, permanently, for that handle (AGENTS.md
            landmine #3).
        """
        ...

    def warmup_flag(self) -> bool:
        """Whether the simulation is still in warmup convergence.

        Returns:
            ``True`` during warmup. Warmup telemetry is physically meaningless
            and must never be actuated on or enter metrics/LLM context.
        """
        ...

    def is_weather_run_period(self) -> bool:
        """Whether the environment currently executing is the weather-file run period.

        EnergyPlus also runs full zone/system/plant physics — and fires every
        callback registered here — for the *sizing* environments (one or more
        design days, iterated as many times as convergence needs), and those
        timesteps are neither warmup nor meaningful telemetry: they describe a
        design day that may not even fall within the requested run period.
        Without this check, the reflex tier builds and publishes a sample for
        every sizing timestep too, which for an autosized system can outnumber
        the real run period's timesteps by an order of magnitude and does
        nothing but waste CPU on samples nobody asked for.

        Returns:
            ``True`` only during the actual weather-file run period.
        """
        ...

    def current_environment_name(self) -> str:
        """The name of the environment currently executing.

        Returns:
            E.g. a sizing period's name, or the run period's name. Used to
            detect environment transitions so per-environment accumulators can
            be reset rather than blended (AGENTS.md landmine #7).
        """
        ...

    def clock(self) -> tuple[int, int, int, int, int, int]:
        """The simulation's current calendar position.

        Returns:
            ``(year, month, day, hour, minute, day_of_week)``. ``hour`` is
            normalised to ``0-23``; EnergyPlus itself reports ``1-24``, which
            :mod:`ecoloop.bus.models` deliberately does not expose.
        """
        ...

    # -- callback registration ------------------------------------------------ #
    def on_begin_new_environment(self, callback: Callable[[], None]) -> None:
        """Register a callback to run once at the start of each environment.

        Args:
            callback: A zero-argument callable. Exceptions raised inside it
                must not propagate — see AGENTS.md invariant #1.
        """
        ...

    def on_end_zone_timestep(self, callback: Callable[[], None]) -> None:
        """Register a callback to run at the end of every zone timestep.

        This is the reflex tier's hook: telemetry is read and actuation
        applied here, after EnergyPlus has finished its own reporting for the
        timestep.

        Args:
            callback: A zero-argument callable. Exceptions raised inside it
                must not propagate — see AGENTS.md invariant #1.
        """
        ...
