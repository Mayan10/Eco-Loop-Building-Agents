"""A lumped-capacitance thermal double implementing SimulationBackend.

This is what makes the entire control stack testable without a real
EnergyPlus install: it understands the exact variable, meter and actuator
vocabulary declared in ``config/zones.yaml`` — the same names
:mod:`ecoloop.simulation.handles` resolves against the real engine — so
:class:`~ecoloop.simulation.handles.HandleRegistry`,
:class:`~ecoloop.simulation.callbacks.ReflexCallbacks`, and every controller
in ``control/`` can run their real, unmodified code against this fake instead.

**What the physics are, and are not.** Each zone is a single thermal mass
losing or gaining heat to a sinusoidal outdoor temperature, with an ideal
HVAC system that can add or remove heat up to a capacity limit to chase the
active setpoints. PMV is a linear approximation around a neutral temperature
— not the Fanger equation — but PPD is computed from that PMV with the real
ASHRAE relationship, so the "PPD must track PMV" invariant a test might check
is still genuine. This is a control-logic test double, not a competing
building simulator: it exists to make *this project's* decision logic
provably correct, not to model heat transfer precisely.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ecoloop.simulation.backend import INVALID_HANDLE

__all__ = ["FakeEnergyPlusBackend", "FakeRunConfig", "FakeZoneConfig"]

_SECONDS_PER_HOUR = 3600.0
_NEUTRAL_TEMP_C = 24.0
_PMV_SPAN_C = 6.0
_CO2_OUTDOOR_PPM = 400.0
_CO2_GENERATION_PPM_PER_HOUR_OCCUPIED = 300.0
_CO2_DECAY_PER_HOUR = 1.5


@dataclass(frozen=True, slots=True)
class FakeZoneConfig:
    """Thermal and occupancy parameters for one fake zone."""

    name: str
    conditioned: bool = True
    people_name: str | None = None
    thermal_mass_j_per_k: float = 5.0e7
    ua_w_per_k: float = 200.0
    initial_temp_c: float = 22.0
    max_heating_w: float = 8_000.0
    max_cooling_w: float = 8_000.0
    cooling_cop: float = 3.0
    internal_gains_w_occupied: float = 500.0


@dataclass(frozen=True, slots=True)
class FakeRunConfig:
    """Everything the fake needs to drive its own physics loop.

    Unlike the real engine, this never reads an IDF or EPW — ``run()``'s
    ``idf_path``/``weather_path`` arguments are accepted (to satisfy the
    protocol) and ignored.
    """

    zones: tuple[FakeZoneConfig, ...]
    timesteps_per_hour: int = 6
    total_hours: int = 336
    warmup_timesteps: int = 6
    outdoor_temp_mean_c: float = 24.0
    outdoor_temp_amplitude_c: float = 8.0
    occupied_start_hour: float = 8.0
    occupied_end_hour: float = 18.0


def _occupancy_fraction(hour_of_day: float, config: FakeRunConfig) -> float:
    """Whether the building is occupied at a given hour of day.

    Args:
        hour_of_day: Hour, 0.0-24.0.
        config: The run configuration.

    Returns:
        ``1.0`` if occupied, ``0.0`` otherwise. A step function, not a ramp —
        simple and sufficient for exercising the setback/economiser logic.
    """
    return 1.0 if config.occupied_start_hour <= hour_of_day < config.occupied_end_hour else 0.0


def _outdoor_temperature_c(hour_of_day: float, config: FakeRunConfig) -> float:
    """A sinusoidal outdoor temperature, peaking mid-afternoon.

    Args:
        hour_of_day: Hour, 0.0-24.0.
        config: The run configuration.

    Returns:
        Outdoor dry-bulb temperature in Celsius.
    """
    phase = (hour_of_day - 15.0) / 24.0 * 2.0 * math.pi
    return config.outdoor_temp_mean_c + config.outdoor_temp_amplitude_c * math.cos(phase)


def _ppd_from_pmv(pmv: float) -> float:
    """The real ASHRAE 55 PPD-from-PMV relationship.

    Args:
        pmv: Predicted Mean Vote.

    Returns:
        Predicted Percentage Dissatisfied, 5-100.
    """
    return 100.0 - 95.0 * math.exp(-(0.03353 * pmv**4 + 0.2179 * pmv**2))


@dataclass(slots=True)
class _ZoneState:
    """Mutable per-zone simulation state."""

    temp_c: float
    co2_ppm: float = _CO2_OUTDOOR_PPM
    heating_setpoint_c: float = 21.0
    cooling_setpoint_c: float = 24.0


class FakeEnergyPlusBackend:
    """A SimulationBackend implementation with no dependency on EnergyPlus."""

    def __init__(self, config: FakeRunConfig) -> None:
        """Set up initial zone state and an empty callback registry."""
        self._config = config
        self._zone_state = {z.name: _ZoneState(temp_c=z.initial_temp_c) for z in config.zones}
        self._meters: dict[str, float] = {
            "ElectricityNet:Facility": 0.0,
            "NaturalGas:Facility": 0.0,
            "Cooling:Electricity": 0.0,
            "Heating:NaturalGas": 0.0,
            "Heating:Electricity": 0.0,
            "Fans:Electricity": 0.0,
            "InteriorLights:Electricity": 0.0,
            "InteriorEquipment:Electricity": 0.0,
        }
        self._requested: set[tuple[str, str]] = set()
        self._begin_callbacks: list[Callable[[], None]] = []
        self._timestep_callbacks: list[Callable[[], None]] = []
        self._timestep_index = 0
        self._ready = False
        self._warmup = True
        self._environment_name = "environment-1"
        self._year, self._month, self._day, self._hour, self._minute = 1999, 1, 1, 0, 0
        self._day_of_week = 6

    # -- test-only helpers, not part of the protocol ------------------------ #

    def zone_temperature(self, name: str) -> float:
        """Read a zone's current temperature, for test assertions."""
        return self._zone_state[name].temp_c

    def meter_total(self, name: str) -> float:
        """Read a meter's cumulative total, for test assertions."""
        return self._meters[name]

    def zone_setpoints(self, name: str) -> tuple[float, float]:
        """Read a zone's currently-actuated (heating, cooling) setpoints.

        The direct way to prove a controller's decision actually reached
        ``set_actuator_value`` rather than just being computed: unlike zone
        temperature, which the fake's ideal-HVAC physics pulls toward a
        setpoint regardless of whether anything ever actuated it, these
        values only change when something calls the actuator.
        """
        state = self._zone_state[name]
        return state.heating_setpoint_c, state.cooling_setpoint_c

    # -- SimulationBackend protocol ------------------------------------------ #

    def run(self, *, idf_path: Path, weather_path: Path, output_dir: Path) -> int:
        """Drive the fake physics loop for the configured run period.

        Args:
            idf_path: Ignored.
            weather_path: Ignored.
            output_dir: Ignored.

        Returns:
            Always ``0``.
        """
        del idf_path, weather_path, output_dir
        for callback in self._begin_callbacks:
            callback()

        total_timesteps = self._config.total_hours * self._config.timesteps_per_hour
        dt_seconds = _SECONDS_PER_HOUR / self._config.timesteps_per_hour

        for _ in range(total_timesteps):
            self._advance_clock(dt_seconds)
            self._ready = True
            self._warmup = self._timestep_index < self._config.warmup_timesteps
            self._timestep_index += 1
            self._step_physics(dt_seconds)
            for callback in self._timestep_callbacks:
                callback()
        return 0

    def request_variable(self, variable: str, key: str) -> None:
        """See SimulationBackend.request_variable."""
        self._requested.add((variable, key))

    def get_variable_handle(self, variable: str, key: str) -> int:
        """See SimulationBackend.get_variable_handle."""
        if self._resolve_variable(variable, key) is None:
            return INVALID_HANDLE
        return hash((variable, key)) & 0x7FFFFFFF

    def get_variable_value(self, handle: int) -> float:
        """See SimulationBackend.get_variable_value."""
        for variable, key in self._all_variable_points():
            if (hash((variable, key)) & 0x7FFFFFFF) == handle:
                value = self._resolve_variable(variable, key)
                if value is not None:
                    return value
        raise KeyError(f"unresolved handle: {handle}")

    def get_meter_handle(self, meter: str) -> int:
        """See SimulationBackend.get_meter_handle."""
        if meter not in self._meters:
            return INVALID_HANDLE
        return hash(("meter", meter)) & 0x7FFFFFFF

    def get_meter_value(self, handle: int) -> float:
        """See SimulationBackend.get_meter_value."""
        for name in self._meters:
            if (hash(("meter", name)) & 0x7FFFFFFF) == handle:
                return self._meters[name]
        raise KeyError(f"unresolved meter handle: {handle}")

    def get_actuator_handle(self, component_type: str, control_type: str, key: str) -> int:
        """See SimulationBackend.get_actuator_handle."""
        if component_type != "Zone Temperature Control" or key not in self._zone_state:
            return INVALID_HANDLE
        if control_type not in ("Heating Setpoint", "Cooling Setpoint"):
            return INVALID_HANDLE
        return hash(("actuator", component_type, control_type, key)) & 0x7FFFFFFF

    def set_actuator_value(self, handle: int, value: float) -> None:
        """See SimulationBackend.set_actuator_value."""
        for name in self._zone_state:
            for control_type in ("Heating Setpoint", "Cooling Setpoint"):
                candidate = hash(("actuator", "Zone Temperature Control", control_type, name))
                if (candidate & 0x7FFFFFFF) == handle:
                    if control_type == "Heating Setpoint":
                        self._zone_state[name].heating_setpoint_c = value
                    else:
                        self._zone_state[name].cooling_setpoint_c = value
                    return
        raise KeyError(f"unresolved actuator handle: {handle}")

    def api_data_fully_ready(self) -> bool:
        """See SimulationBackend.api_data_fully_ready."""
        return self._ready

    def warmup_flag(self) -> bool:
        """See SimulationBackend.warmup_flag."""
        return self._warmup

    def is_weather_run_period(self) -> bool:
        """See SimulationBackend.is_weather_run_period.

        The fake has no separate sizing environment, so every timestep past
        warmup is the run period.
        """
        return not self._warmup

    def current_environment_name(self) -> str:
        """See SimulationBackend.current_environment_name."""
        return self._environment_name

    def clock(self) -> tuple[int, int, int, int, int, int]:
        """See SimulationBackend.clock."""
        return (self._year, self._month, self._day, self._hour, self._minute, self._day_of_week)

    def on_begin_new_environment(self, callback: Callable[[], None]) -> None:
        """See SimulationBackend.on_begin_new_environment."""
        self._begin_callbacks.append(callback)

    def on_end_zone_timestep(self, callback: Callable[[], None]) -> None:
        """See SimulationBackend.on_end_zone_timestep."""
        self._timestep_callbacks.append(callback)

    # -- internal physics ---------------------------------------------------- #

    def _advance_clock(self, dt_seconds: float) -> None:
        """Move the fake calendar forward by one timestep."""
        total_minutes = self._hour * 60 + self._minute + int(dt_seconds / 60)
        self._day_of_week = (
            self._day_of_week if total_minutes < 24 * 60 else (self._day_of_week % 7) + 1
        )
        while total_minutes >= 24 * 60:
            total_minutes -= 24 * 60
            self._day += 1
        self._hour, self._minute = divmod(total_minutes, 60)

    def _hour_of_day(self) -> float:
        return self._hour + self._minute / 60.0

    def _step_physics(self, dt_seconds: float) -> None:
        """Advance every zone's temperature, CO2, and the energy meters."""
        outdoor_c = _outdoor_temperature_c(self._hour_of_day(), self._config)
        occupied = _occupancy_fraction(self._hour_of_day(), self._config) > 0.5

        for zone_config in self._config.zones:
            state = self._zone_state[zone_config.name]
            heat_loss_w = zone_config.ua_w_per_k * (outdoor_c - state.temp_c)
            gains_w = zone_config.internal_gains_w_occupied if occupied else 0.0

            hvac_w = 0.0
            if zone_config.conditioned:
                if state.temp_c < state.heating_setpoint_c:
                    needed_w = (
                        (state.heating_setpoint_c - state.temp_c)
                        * zone_config.thermal_mass_j_per_k
                        / dt_seconds
                    )
                    hvac_w = min(needed_w, zone_config.max_heating_w)
                    self._meters["Heating:NaturalGas"] += hvac_w * dt_seconds
                    self._meters["NaturalGas:Facility"] += hvac_w * dt_seconds
                elif state.temp_c > state.cooling_setpoint_c:
                    needed_w = (
                        (state.temp_c - state.cooling_setpoint_c)
                        * zone_config.thermal_mass_j_per_k
                        / dt_seconds
                    )
                    cooling_w = min(needed_w, zone_config.max_cooling_w)
                    hvac_w = -cooling_w
                    electrical_w = cooling_w / zone_config.cooling_cop
                    self._meters["Cooling:Electricity"] += electrical_w * dt_seconds
                    self._meters["ElectricityNet:Facility"] += electrical_w * dt_seconds

            state.temp_c += (
                (heat_loss_w + gains_w + hvac_w) * dt_seconds / zone_config.thermal_mass_j_per_k
            )

            hours_elapsed = dt_seconds / _SECONDS_PER_HOUR
            generation = _CO2_GENERATION_PPM_PER_HOUR_OCCUPIED if occupied else 0.0
            decay = _CO2_DECAY_PER_HOUR * (state.co2_ppm - _CO2_OUTDOOR_PPM)
            state.co2_ppm += (generation - decay) * hours_elapsed

            fixture_w = 300.0 if occupied else 0.0
            self._meters["InteriorLights:Electricity"] += fixture_w * dt_seconds
            self._meters["ElectricityNet:Facility"] += fixture_w * dt_seconds

    def _all_variable_points(self) -> list[tuple[str, str]]:
        """Every (variable, key) pair this fake can answer, for handle lookup."""
        points: list[tuple[str, str]] = [
            ("Site Outdoor Air Drybulb Temperature", "Environment"),
            ("Site Outdoor Air Relative Humidity", "Environment"),
        ]
        for zone_config in self._config.zones:
            name = zone_config.name
            points.append(("Zone Mean Air Temperature", name))
            points.append(("Zone Air Relative Humidity", name))
            points.append(("Zone Thermostat Heating Setpoint Temperature", name))
            points.append(("Zone Thermostat Cooling Setpoint Temperature", name))
            points.append(("Zone People Occupant Count", name))
            points.append(("Zone Air CO2 Concentration", name))
            if zone_config.people_name is not None:
                points.append(("Zone Thermal Comfort Fanger Model PMV", zone_config.people_name))
                points.append(("Zone Thermal Comfort Fanger Model PPD", zone_config.people_name))
        return points

    def _resolve_variable(self, variable: str, key: str) -> float | None:
        """Compute a variable's current value, or ``None`` if unknown.

        Args:
            variable: EnergyPlus output variable name.
            key: Key value (zone name, People name, or ``"Environment"``).

        Returns:
            The value, or ``None`` if this fake has no such point.
        """
        if variable == "Site Outdoor Air Drybulb Temperature" and key == "Environment":
            return _outdoor_temperature_c(self._hour_of_day(), self._config)
        if variable == "Site Outdoor Air Relative Humidity" and key == "Environment":
            return 50.0

        zone_config = next((z for z in self._config.zones if z.name == key), None)
        state = self._zone_state.get(key)
        if variable in (
            "Zone Thermal Comfort Fanger Model PMV",
            "Zone Thermal Comfort Fanger Model PPD",
        ):
            zone_config = next((z for z in self._config.zones if z.people_name == key), None)
            state = self._zone_state.get(zone_config.name) if zone_config else None

        if zone_config is None or state is None:
            return None

        occupied = _occupancy_fraction(self._hour_of_day(), self._config) > 0.5
        if variable == "Zone Mean Air Temperature":
            return state.temp_c
        if variable == "Zone Air Relative Humidity":
            return 45.0
        if variable == "Zone Thermostat Heating Setpoint Temperature":
            return state.heating_setpoint_c
        if variable == "Zone Thermostat Cooling Setpoint Temperature":
            return state.cooling_setpoint_c
        if variable == "Zone People Occupant Count":
            return 1.0 if occupied else 0.0
        if variable == "Zone Air CO2 Concentration":
            return state.co2_ppm
        if variable == "Zone Thermal Comfort Fanger Model PMV":
            pmv = (state.temp_c - _NEUTRAL_TEMP_C) / (_PMV_SPAN_C / 3.0)
            return max(-3.0, min(3.0, pmv))
        if variable == "Zone Thermal Comfort Fanger Model PPD":
            pmv = (state.temp_c - _NEUTRAL_TEMP_C) / (_PMV_SPAN_C / 3.0)
            return _ppd_from_pmv(max(-3.0, min(3.0, pmv)))
        return None
