"""A thin, typed wrapper around eppy for reading and editing IDF files.

eppy's ``IDF`` class binds its parsed IDD to a **class attribute**, not an
instance attribute: the first call to ``IDF.setiddname()`` in a process fixes
which EnergyPlus version's schema every subsequent ``IDF(...)`` is parsed
against, and a second call with a *different* path raises. Since Eco-Loop only
ever targets the one discovered installation, :func:`set_idd` makes that
binding idempotent — safe to call once per module that needs it, without every
caller needing to remember eppy's one-IDD-per-process rule.

This module has no opinion about *what* to inject; it only knows how to load
an IDF, look things up case-insensitively (EnergyPlus upper-cases identifiers;
AGENTS.md landmine), and save. :mod:`ecoloop.simulation.prepare` is where the
comfort and CO2 injection policy lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecoloop.errors import IDFValidationError
from ecoloop.logging import get_logger

__all__ = [
    "get_object_field",
    "has_object_field_value",
    "load_idf",
    "people_comfort_model",
    "save_idf",
    "set_idd",
    "set_object_field",
]

_logger = get_logger(__name__, component="simulation")

_FANGER = "FANGER"


def set_idd(idd_path: Path) -> None:
    """Bind eppy to an IDD, tolerating repeated calls with the same path.

    Args:
        idd_path: Path to ``Energy+.idd`` inside an EnergyPlus install.

    Raises:
        IDFValidationError: If eppy is already bound to a *different* IDD in
            this process. Eco-Loop never needs this — one process targets one
            discovered installation — so it signals a programming error rather
            than something to recover from.
    """
    from eppy.modeleditor import IDF
    from eppy.modeleditor import IDDAlreadySetError as _IDDAlreadySetError

    try:
        IDF.setiddname(str(idd_path))
    except _IDDAlreadySetError as exc:
        raise IDFValidationError(
            "eppy is already bound to a different IDD in this process",
            requested=str(idd_path),
            bound=str(IDF.iddname),
        ) from exc


def load_idf(idf_path: Path) -> Any:
    """Parse an IDF file.

    Args:
        idf_path: Path to the IDF. :func:`set_idd` must have been called
            first, with a matching EnergyPlus version.

    Returns:
        An ``eppy.modeleditor.IDF`` instance. Typed as ``Any`` deliberately:
        eppy's IDF is a dynamically-generated class with no useful static
        shape, and re-declaring one here would just be a second source of
        truth for the schema.

    Raises:
        IDFValidationError: If the file does not exist or fails to parse.
    """
    if not idf_path.is_file():
        raise IDFValidationError("IDF file not found", path=str(idf_path))
    from eppy.modeleditor import IDF

    try:
        return IDF(str(idf_path))
    except Exception as exc:
        raise IDFValidationError("IDF failed to parse", path=str(idf_path), cause=str(exc)) from exc


def get_object_field(idf: Any, object_type: str, object_name: str, field: str) -> str | None:
    """Read one field from a named IDF object, matching names case-insensitively.

    Args:
        idf: A loaded IDF, from :func:`load_idf`.
        object_type: IDD object type, e.g. ``"PEOPLE"``.
        object_name: The object's ``Name`` field value.
        field: eppy field attribute name, e.g. ``"Thermal_Comfort_Model_1_Type"``.

    Returns:
        The field value as a string, or ``None`` if no object of that type and
        name exists.
    """
    wanted = object_name.strip().upper()
    for obj in idf.idfobjects[object_type.upper()]:
        if obj.Name.strip().upper() == wanted:
            return str(getattr(obj, field, "") or "")
    return None


def people_comfort_model(idf: Any, people_name: str) -> str | None:
    """Read a People object's declared thermal comfort model.

    Args:
        idf: A loaded IDF.
        people_name: The People object's name (not the zone name).

    Returns:
        The comfort model string (e.g. ``"FANGER"``), upper-cased for
        comparison, or ``None`` if the object does not exist. EnergyPlus
        upper-cases this field regardless of how the IDF source wrote it, so a
        naive case-sensitive ``"Fanger"`` comparison against it always fails.
    """
    value = get_object_field(idf, "PEOPLE", people_name, "Thermal_Comfort_Model_1_Type")
    return None if value is None else value.strip().upper()


def set_object_field(idf: Any, object_type: str, object_name: str, field: str, value: str) -> None:
    """Set one field on a named IDF object, matching the name case-insensitively.

    Args:
        idf: A loaded IDF.
        object_type: IDD object type, e.g. ``"PEOPLE"``.
        object_name: The object's ``Name`` field value.
        field: eppy field attribute name to set.
        value: New value.

    Raises:
        IDFValidationError: If no object of that type and name exists.
    """
    wanted = object_name.strip().upper()
    for obj in idf.idfobjects[object_type.upper()]:
        if obj.Name.strip().upper() == wanted:
            setattr(obj, field, value)
            return
    raise IDFValidationError(
        "cannot set field on missing object", object_type=object_type, name=object_name
    )


def has_object_field_value(idf: Any, object_type: str, field: str, value: str) -> bool:
    """Whether any object of a type has a given field value, case-insensitively.

    Args:
        idf: A loaded IDF.
        object_type: IDD object type, e.g. ``"OUTPUT:VARIABLE"``.
        field: eppy field attribute name to inspect.
        value: Value to match, case-insensitively.

    Returns:
        ``True`` if at least one matching object exists.
    """
    wanted = value.strip().upper()
    return any(
        str(getattr(obj, field, "")).strip().upper() == wanted
        for obj in idf.idfobjects[object_type.upper()]
    )


def save_idf(idf: Any, destination: Path) -> Path:
    """Write an IDF to a new location, creating parent directories as needed.

    Args:
        idf: A loaded (and possibly modified) IDF.
        destination: Path to write to.

    Returns:
        The destination path, for chaining.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    idf.saveas(str(destination))
    _logger.info("wrote IDF", path=str(destination))
    return destination
