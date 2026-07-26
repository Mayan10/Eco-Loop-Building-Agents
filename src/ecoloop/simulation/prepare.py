"""Turn the baseline IDF into the one Eco-Loop actually simulates.

Two things are genuinely absent from ``models/baseline/small_office.idf`` and
must be injected before a run can report PMV/PPD or CO2 at all:

1. **``ZoneAirContaminantBalance``.** CO2 concentration is not tracked unless
   this singular object exists with its ``Carbon Dioxide Concentration`` flag
   set to ``Yes`` — it is a simulation feature switch, not an output request,
   and the baseline IDF has none.
2. **Explicit ``Output:Variable`` entries for PMV, PPD, and CO2.** The Fanger
   model itself is *already* requested — all five ``People`` objects declare
   ``FANGER`` (AGENTS.md corrects an earlier false claim to the contrary) — so
   this step is not what makes PMV computable. What it does is put those
   series into the post-run ``.eso`` file, which is what ``analysis/`` and a
   human with a spreadsheet both read. The live reflex tier does not need
   this: EnergyPlus's ``request_variable()`` data-exchange call makes a
   variable readable at runtime whether or not an ``Output:Variable`` object
   exists for it in the IDF.

**A third thing is not absent but actively wrong, and is the one that matters
most.** The baseline IDF's ``SimulationControl`` object ships with ``Run
Simulation for Weather File Run Periods = No``. DOE reference models are
distributed this way because they are meant for sizing studies, not for
annual energy simulation — but it means that, unpatched, EnergyPlus runs only
the design-day sizing periods and *never executes the weather-file run period
at all*. Every meter and variable resolves and reads back zero for the entire
run, silently, because nothing is wrong with the handles — the timesteps
being sampled just are not the ones anyone asked for. ``prepare_idf`` flips
this to ``Yes`` and turns ``Run Simulation for Sizing Periods`` to ``No``
(autosizing itself is controlled by the separate ``Do Zone/System/Plant
Sizing Calculation`` flags and is unaffected, so equipment capacities are
unchanged; only the redundant sizing-period *simulation output* is skipped).

**A fourth thing is not wrong so much as simply never applied.** The IDF's
``RunPeriod`` object ships spanning the full calendar year, and nothing about
selecting ``--profile fast`` or ``--profile demo`` touches it — those profiles
only narrow ``config/``'s ``simulation.run_period``, which nothing previously
read back into the IDF. Left alone, *every* profile silently simulated the
same full year: `fast`'s entire reason to exist — finishing in minutes while
iterating, not the better part of an hour — never took effect.
``prepare_idf`` copies the active profile's begin/end month and day onto the
``RunPeriod`` object so the two cannot drift apart.

``prepare_idf`` is idempotent: every injection checks for its own prior
existence first, so running it twice against an already-prepared IDF is a
no-op, not a duplicate-object error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecoloop.config import EcoLoopSettings, RunPeriod
from ecoloop.errors import IDFValidationError
from ecoloop.logging import get_logger
from ecoloop.simulation.handles import ZoneMap, load_zone_map
from ecoloop.simulation.idf import (
    has_object_field_value,
    load_idf,
    people_comfort_model,
    save_idf,
    set_idd,
    set_object_field,
)
from ecoloop.simulation.locate import discover_energyplus

__all__ = ["has_pmv_output", "prepare_idf"]

_logger = get_logger(__name__, component="simulation")

_FANGER = "FANGER"
_PMV_VARIABLE = "Zone Thermal Comfort Fanger Model PMV"
_PPD_VARIABLE = "Zone Thermal Comfort Fanger Model PPD"
_CO2_VARIABLE = "Zone Air CO2 Concentration"
_OUTDOOR_CO2_SCHEDULE_NAME = "Outdoor CO2 Schedule"
_REPORTING_FREQUENCY = "Timestep"


def prepare_idf(
    settings: EcoLoopSettings,
    *,
    idf_path: Path | None = None,
    zone_map_path: Path | None = None,
) -> Path:
    """Prepare the baseline IDF and write it to ``simulation.idf_prepared``.

    Args:
        settings: Loaded Eco-Loop settings.
        idf_path: Override for the source IDF; defaults to
            ``simulation.idf_baseline``.
        zone_map_path: Override for the zone map; defaults to
            ``config/zones.yaml``.

    Returns:
        The path the prepared IDF was written to.
    """
    install = discover_energyplus(settings.simulation.energyplus_dir)
    set_idd(install.root / "Energy+.idd")

    source = settings.resolve(idf_path or settings.simulation.idf_baseline)
    idf = load_idf(source)
    zone_map = load_zone_map(zone_map_path or settings.resolve(Path("config/zones.yaml")))

    _ensure_weather_run_period_enabled(idf)
    _ensure_run_period_dates(idf, settings.simulation.run_period)
    _ensure_fanger_comfort_model(idf, zone_map)
    _ensure_contaminant_balance(idf, outdoor_co2_ppm=settings.comfort.outdoor_co2_ppm)
    _ensure_output_variables(idf, zone_map)

    destination = settings.resolve(settings.simulation.idf_prepared)
    save_idf(idf, destination)
    _logger.info("prepared IDF", source=str(source), destination=str(destination))
    return destination


def _ensure_weather_run_period_enabled(idf: Any) -> None:
    """Force the weather-file run period to actually execute.

    The DOE reference model this project vendors ships configured for sizing
    studies only. Left alone, EnergyPlus runs the design-day sizing
    environments and reports success — ``rc == 0``, no Severe errors — having
    never touched the weather file at all, which makes every meter and
    variable read back as a sizing-period artifact rather than the annual (or
    fast-profile) run anyone actually asked for.

    Args:
        idf: A loaded IDF.

    Raises:
        IDFValidationError: If the IDF has no ``SimulationControl`` object.
            EnergyPlus itself requires exactly one; an IDF missing it would
            fail its own input processing before this code ever ran, so this
            is a defensive check, not the primary validation.
    """
    controls = idf.idfobjects["SIMULATIONCONTROL"]
    if not controls:
        raise IDFValidationError("IDF has no SimulationControl object")
    control = controls[0]

    if control.Run_Simulation_for_Weather_File_Run_Periods.strip().upper() != "YES":
        control.Run_Simulation_for_Weather_File_Run_Periods = "Yes"
        _logger.warning("baseline IDF had weather-file run periods disabled; forcing them on")
    if control.Run_Simulation_for_Sizing_Periods.strip().upper() != "NO":
        control.Run_Simulation_for_Sizing_Periods = "No"


def _ensure_run_period_dates(idf: Any, run_period: RunPeriod) -> None:
    """Force the IDF's ``RunPeriod`` object to match the active profile.

    The baseline IDF ships one ``RunPeriod`` spanning the full calendar year.
    Nothing else in the pipeline narrows it: ``--profile fast`` and
    ``--profile demo`` only exist in ``config/``, so without this, every
    profile silently ran the same annual period regardless of what
    ``simulation.run_period`` said — the fast profile's entire reason to
    exist (finishing in minutes, not hours, while iterating) never took
    effect, and every run paid the full annual cost.

    Args:
        idf: A loaded IDF.
        run_period: The active profile's run period, from
            ``simulation.run_period``.

    Raises:
        IDFValidationError: If the IDF has no ``RunPeriod`` object.
    """
    periods = idf.idfobjects["RUNPERIOD"]
    if not periods:
        raise IDFValidationError("IDF has no RunPeriod object")
    period = periods[0]
    period.Begin_Month = run_period.begin_month
    period.Begin_Day_of_Month = run_period.begin_day
    period.End_Month = run_period.end_month
    period.End_Day_of_Month = run_period.end_day


def _ensure_fanger_comfort_model(idf: Any, zone_map: ZoneMap) -> None:
    """Force Fanger comfort reporting on every occupied zone's People object.

    Defensive rather than load-bearing for the shipped IDF, which already
    declares it — but ``zones.yaml``'s point map is meant to travel to a
    different building model, and that model may not.

    Args:
        idf: A loaded IDF.
        zone_map: The zone point map naming each zone's People object.
    """
    for zone in zone_map.zones:
        if zone.people_object is None:
            continue
        current = people_comfort_model(idf, zone.people_object)
        if current == _FANGER:
            continue
        set_object_field(
            idf, "PEOPLE", zone.people_object, "Thermal_Comfort_Model_1_Type", "Fanger"
        )
        _logger.info("enabled Fanger comfort model", people_object=zone.people_object)


def _ensure_contaminant_balance(idf: Any, *, outdoor_co2_ppm: float) -> None:
    """Inject ``ZoneAirContaminantBalance`` if the IDF has none.

    Args:
        idf: A loaded IDF.
        outdoor_co2_ppm: Background CO2 concentration for the outdoor
            schedule, from ``comfort.outdoor_co2_ppm``.
    """
    if idf.idfobjects["ZONEAIRCONTAMINANTBALANCE"]:
        return

    if not any(
        obj.Name.strip().upper() == _OUTDOOR_CO2_SCHEDULE_NAME.upper()
        for obj in idf.idfobjects["SCHEDULE:CONSTANT"]
    ):
        idf.newidfobject(
            "SCHEDULE:CONSTANT",
            Name=_OUTDOOR_CO2_SCHEDULE_NAME,
            Hourly_Value=outdoor_co2_ppm,
        )

    idf.newidfobject(
        "ZONEAIRCONTAMINANTBALANCE",
        Carbon_Dioxide_Concentration="Yes",
        Outdoor_Carbon_Dioxide_Schedule_Name=_OUTDOOR_CO2_SCHEDULE_NAME,
    )
    _logger.info("injected ZoneAirContaminantBalance", outdoor_co2_ppm=outdoor_co2_ppm)


def _ensure_output_variables(idf: Any, zone_map: ZoneMap) -> None:
    """Add explicit Output:Variable entries for comfort and CO2 reporting.

    Args:
        idf: A loaded IDF.
        zone_map: The zone point map naming each zone's People object.
    """
    for zone in zone_map.zones:
        if zone.people_object is not None:
            _ensure_output_variable(idf, _PMV_VARIABLE, zone.people_object)
            _ensure_output_variable(idf, _PPD_VARIABLE, zone.people_object)
        if zone.conditioned:
            _ensure_output_variable(idf, _CO2_VARIABLE, zone.name)


def _ensure_output_variable(idf: Any, variable: str, key: str) -> None:
    """Add one Output:Variable object, unless an equivalent one already exists.

    Args:
        idf: A loaded IDF.
        variable: EnergyPlus output variable name.
        key: Key value for the variable.
    """
    wanted_key = key.strip().upper()
    wanted_variable = variable.strip().upper()
    exists = any(
        obj.Key_Value.strip().upper() in (wanted_key, "*")
        and obj.Variable_Name.strip().upper() == wanted_variable
        for obj in idf.idfobjects["OUTPUT:VARIABLE"]
    )
    if exists:
        return
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value=key,
        Variable_Name=variable,
        Reporting_Frequency=_REPORTING_FREQUENCY,
    )


def has_pmv_output(idf: Any) -> bool:
    """Whether the IDF already requests PMV output for at least one zone.

    Used by ``ecoloop prepare --verify`` to confirm injection succeeded.

    Args:
        idf: A loaded IDF.

    Returns:
        ``True`` if any ``Output:Variable`` requests the PMV series.
    """
    return has_object_field_value(idf, "OUTPUT:VARIABLE", "Variable_Name", _PMV_VARIABLE)
