"""A minimal SimulationBackend stub for unit-testing handles.py and callbacks.py.

This is deliberately not the lumped-capacitance thermal double described for
Phase 4 (``tests/fakes/fake_energyplus.py``) — it has no physics at all. It
exists only to prove that handle resolution and callback wiring behave
correctly against *anything* satisfying the protocol, which is the entire
point of the protocol existing.
"""

from __future__ import annotations

from collections.abc import Callable

from ecoloop.simulation.backend import INVALID_HANDLE


class FakeBackend:
    """A bare-bones SimulationBackend double, driven entirely by test code."""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []
        self._variable_handles: dict[tuple[str, str], int] = {}
        self._variable_values: dict[int, float] = {}
        self._meter_handles: dict[str, int] = {}
        self._meter_values: dict[int, float] = {}
        self._actuator_handles: dict[tuple[str, str, str], int] = {}
        self._actuator_values: dict[int, float] = {}
        self._next_handle = 0
        self.ready = False
        self.warmup = False
        self.weather_run_period = True
        self.environment_name = "environment-1"
        self.clock_value: tuple[int, int, int, int, int, int] = (1999, 1, 1, 0, 0, 6)
        self._on_begin: Callable[[], None] | None = None
        self._on_timestep: Callable[[], None] | None = None

    def _allocate(self) -> int:
        handle = self._next_handle
        self._next_handle += 1
        return handle

    def allow_variable(self, variable: str, key: str, value: float) -> None:
        """Make a variable resolvable, with a given initial value."""
        handle = self._allocate()
        self._variable_handles[(variable, key)] = handle
        self._variable_values[handle] = value

    def set_variable_value(self, variable: str, key: str, value: float) -> None:
        handle = self._variable_handles[(variable, key)]
        self._variable_values[handle] = value

    def allow_meter(self, meter: str, value: float) -> None:
        """Make a meter resolvable, with a given initial value."""
        handle = self._allocate()
        self._meter_handles[meter] = handle
        self._meter_values[handle] = value

    def allow_actuator(self, component_type: str, control_type: str, key: str) -> None:
        """Make an actuator resolvable."""
        handle = self._allocate()
        self._actuator_handles[(component_type, control_type, key)] = handle
        self._actuator_values[handle] = 0.0

    def fire_begin_new_environment(self) -> None:
        assert self._on_begin is not None
        self._on_begin()

    def fire_end_zone_timestep(self) -> None:
        assert self._on_timestep is not None
        self._on_timestep()

    # -- SimulationBackend protocol ---------------------------------------- #

    def run(self, *, idf_path: object, weather_path: object, output_dir: object) -> int:
        raise NotImplementedError("FakeBackend does not simulate a real run")

    def request_variable(self, variable: str, key: str) -> None:
        self.requested.append((variable, key))

    def get_variable_handle(self, variable: str, key: str) -> int:
        return self._variable_handles.get((variable, key), INVALID_HANDLE)

    def get_variable_value(self, handle: int) -> float:
        return self._variable_values[handle]

    def get_meter_handle(self, meter: str) -> int:
        return self._meter_handles.get(meter, INVALID_HANDLE)

    def get_meter_value(self, handle: int) -> float:
        return self._meter_values[handle]

    def get_actuator_handle(self, component_type: str, control_type: str, key: str) -> int:
        return self._actuator_handles.get((component_type, control_type, key), INVALID_HANDLE)

    def set_actuator_value(self, handle: int, value: float) -> None:
        self._actuator_values[handle] = value

    def api_data_fully_ready(self) -> bool:
        return self.ready

    def warmup_flag(self) -> bool:
        return self.warmup

    def is_weather_run_period(self) -> bool:
        return self.weather_run_period

    def current_environment_name(self) -> str:
        return self.environment_name

    def clock(self) -> tuple[int, int, int, int, int, int]:
        return self.clock_value

    def on_begin_new_environment(self, callback: Callable[[], None]) -> None:
        self._on_begin = callback

    def on_end_zone_timestep(self, callback: Callable[[], None]) -> None:
        self._on_timestep = callback
