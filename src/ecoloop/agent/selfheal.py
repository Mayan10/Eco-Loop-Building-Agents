"""Diagnose a fatal EnergyPlus input error and attempt a bounded, targeted repair.

This is deliberately narrow, not a generic EnergyPlus error whisperer:
``models/faults/`` exists for one demo (AGENTS.md §12 — "fixtures for the
self-healing demo, do not fix them"), and the honest scope for this module is
the fault class that demo actually injects: an object referencing a schedule
name that does not exist. EnergyPlus reports this at input-processing time,
before any simulation begins, as a distinctive "item not found" pattern —
recognising it and proposing a fix is a real, bounded capability; claiming to
repair arbitrary EnergyPlus failures would not be.

**The repair heuristic.** A broken reference like ``HTGSETP_SCH_MISSING`` is
recovered by finding an existing schedule whose name is the *longest prefix*
of the broken one — recovering the exact fault this project injects (a
suffix appended to an otherwise-valid name) without needing to know anything
about *why* the reference broke. It is not a general fuzzy-match against
every possible typo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecoloop.config import EcoLoopSettings
from ecoloop.logging import get_logger
from ecoloop.simulation.energyplus import EnergyPlusBackend
from ecoloop.simulation.errfile import ErrFileSummary, Severity, parse_err_file
from ecoloop.simulation.idf import load_idf, save_idf, set_idd
from ecoloop.simulation.locate import discover_energyplus

__all__ = ["FaultDiagnosis", "SelfHealResult", "diagnose", "repair", "run_with_self_healing"]

_logger = get_logger(__name__, component="agent")

_SCHEDULE_OBJECT_TYPES = ("SCHEDULE:COMPACT", "SCHEDULE:CONSTANT", "SCHEDULE:YEAR", "SCHEDULE:FILE")

# Matches EnergyPlus's folded "<Object Type> = ... <Field> = <Value>, item
# not found." pattern for an invalid schedule-name reference. See errfile.py:
# the "** ~~~ **" continuation line carrying the field/value has already
# been folded into the parent record's message by this point.
#
# Deliberately does NOT try to extract the object's *name*: EnergyPlus's
# message has no delimiter between "<object name>" and "<field label>" (both
# are free-form, space-containing text), so a name like "CORE_ZN DUALSPSCHED"
# is inherently ambiguous to split from a field label like "Heating Setpoint
# Temperature Schedule Name" by pattern alone — a naive attempt reliably
# mis-split the two in testing. Locating the exact object instance is done
# by searching the IDF for the one whose field currently equals bad_value
# instead (see _find_object_with_field_value), which is unambiguous.
#
# `field` is a closed alternation of known field labels, not an open
# character class: an open class like `[\w ]*?` has the identical problem
# one level down — since the object name is *also* words separated by
# spaces, a permissive class greedily (or, non-greedily but still
# incorrectly) swallows the object name into the field-name capture instead
# of stopping at the real field label. Longer, more specific labels are
# listed before the generic "Schedule Name" fallback so the alternation
# prefers the precise match.
_KNOWN_SCHEDULE_FIELDS = (
    "Heating Setpoint Temperature Schedule Name",
    "Cooling Setpoint Temperature Schedule Name",
    "Schedule Name",
)
_INVALID_SCHEDULE_REFERENCE_RE = re.compile(
    r"(?P<object_type>[A-Za-z]+:[A-Za-z:]+)\s*=.*?"
    r"(?P<field>" + "|".join(_KNOWN_SCHEDULE_FIELDS) + r")"
    r"\s*=\s*(?P<bad_value>\S+?),\s*item not found",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FaultDiagnosis:
    """One recognised, fixable fault extracted from a ``.err`` file."""

    fault_class: str
    object_type: str
    field: str
    bad_value: str


@dataclass(frozen=True, slots=True)
class SelfHealResult:
    """The outcome of a bounded diagnose-patch-rerun loop."""

    succeeded: bool
    attempts: int
    diagnoses: tuple[FaultDiagnosis, ...]
    final_idf_path: Path
    message: str


def diagnose(summary: ErrFileSummary) -> FaultDiagnosis | None:
    """Look for a recognised, fixable fault in a parsed ``.err`` summary.

    Args:
        summary: The parsed ``.err`` file from a failed run.

    Returns:
        A diagnosis if a known fault pattern is found among the Severe
        records, otherwise ``None`` — meaning this failure is outside this
        module's narrow, honest scope.
    """
    for record in summary.filter(Severity.SEVERE):
        match = _INVALID_SCHEDULE_REFERENCE_RE.search(record.message)
        if match:
            return FaultDiagnosis(fault_class="invalid_schedule_reference", **match.groupdict())
    return None


def repair(idf: Any, diagnosis: FaultDiagnosis) -> bool:
    """Attempt to patch a diagnosed fault in place on a loaded IDF.

    Args:
        idf: A loaded IDF (see :func:`ecoloop.simulation.idf.load_idf`).
        diagnosis: The fault to repair.

    Returns:
        ``True`` if a patch was applied, ``False`` if this diagnosis's fault
        class is unrecognised or no repair candidate could be found.
    """
    if diagnosis.fault_class != "invalid_schedule_reference":
        return False

    field_attribute = diagnosis.field.strip().replace(" ", "_")
    target = _find_object_with_field_value(
        idf, diagnosis.object_type, field_attribute, diagnosis.bad_value
    )
    if target is None:
        _logger.warning(
            "no object found with the diagnosed bad value; cannot repair",
            object_type=diagnosis.object_type,
            field=field_attribute,
            bad_value=diagnosis.bad_value,
        )
        return False

    candidate = _find_closest_schedule_name(idf, diagnosis.bad_value)
    if candidate is None:
        _logger.warning(
            "no repair candidate found for invalid schedule reference",
            bad_value=diagnosis.bad_value,
        )
        return False

    setattr(target, field_attribute, candidate)
    _logger.info(
        "repaired invalid schedule reference",
        object_type=diagnosis.object_type,
        object_name=target.Name,
        bad_value=diagnosis.bad_value,
        repaired_value=candidate,
    )
    return True


def _find_object_with_field_value(
    idf: Any, object_type: str, field_attribute: str, value: str
) -> Any | None:
    """Find the object instance whose field currently holds a given value.

    This is how the exact broken object is located: the error text names the
    object *type* and the bad *value* unambiguously, but not the object's
    name (see the regex comment above) — searching for the one instance
    with a matching field value sidesteps that ambiguity entirely.

    Args:
        idf: A loaded IDF.
        object_type: IDD object type, e.g. ``"ThermostatSetpoint:DualSetpoint"``.
        field_attribute: eppy field attribute name.
        value: The value to match, case-insensitively.

    Returns:
        The matching object, or ``None`` if none has that exact value.
    """
    wanted = value.strip().upper()
    for obj in idf.idfobjects[object_type.upper()]:
        if str(getattr(obj, field_attribute, "")).strip().upper() == wanted:
            return obj
    return None


def _find_closest_schedule_name(idf: Any, broken_name: str) -> str | None:
    """Find the existing schedule name that is the longest prefix of a broken one.

    Args:
        idf: A loaded IDF.
        broken_name: The invalid schedule name reference.

    Returns:
        The longest-matching existing schedule name, or ``None`` if no
        existing schedule name is a prefix of ``broken_name``.
    """
    broken_upper = broken_name.strip().upper()
    candidates = [
        obj.Name
        for object_type in _SCHEDULE_OBJECT_TYPES
        for obj in idf.idfobjects[object_type]
        if obj.Name.strip() and broken_upper.startswith(obj.Name.strip().upper())
    ]
    return max(candidates, key=len) if candidates else None


def run_with_self_healing(
    settings: EcoLoopSettings, *, idf_path: Path, weather_path: Path, output_dir: Path
) -> SelfHealResult:
    """Run an IDF, diagnosing and patching recognised faults, up to a retry cap.

    Args:
        settings: Loaded Eco-Loop settings (for EnergyPlus discovery and
            ``agent.selfheal.max_retries``).
        idf_path: The IDF to run — expected to already be prepared (weather
            run period enabled, comfort instrumentation injected).
        weather_path: EPW weather file.
        output_dir: Directory EnergyPlus writes outputs into; overwritten on
            each retry.

    Returns:
        The final outcome: whether the run eventually succeeded, how many
        attempts it took, every diagnosis made along the way, and the path
        of the (possibly patched) IDF that was actually run last.
    """
    install = discover_energyplus(settings.simulation.energyplus_dir)
    set_idd(install.root / "Energy+.idd")

    diagnoses: list[FaultDiagnosis] = []
    current_idf_path = idf_path
    max_attempts = settings.agent.selfheal.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        backend = EnergyPlusBackend(install)
        backend.run(idf_path=current_idf_path, weather_path=weather_path, output_dir=output_dir)

        err_path = output_dir / "eplusout.err"
        summary = parse_err_file(err_path, max_bytes=settings.simulation.output.max_err_bytes)
        if summary.completed_successfully:
            return SelfHealResult(
                succeeded=True,
                attempts=attempt,
                diagnoses=tuple(diagnoses),
                final_idf_path=current_idf_path,
                message=f"succeeded on attempt {attempt} of {max_attempts}",
            )

        diagnosis = diagnose(summary)
        if diagnosis is None:
            return SelfHealResult(
                succeeded=False,
                attempts=attempt,
                diagnoses=tuple(diagnoses),
                final_idf_path=current_idf_path,
                message=f"run failed with no recognised, repairable fault (attempt {attempt})",
            )
        diagnoses.append(diagnosis)

        if attempt == max_attempts:
            return SelfHealResult(
                succeeded=False,
                attempts=attempt,
                diagnoses=tuple(diagnoses),
                final_idf_path=current_idf_path,
                message=f"exhausted {max_attempts} attempts without a successful run",
            )

        idf = load_idf(current_idf_path)
        if not repair(idf, diagnosis):
            return SelfHealResult(
                succeeded=False,
                attempts=attempt,
                diagnoses=tuple(diagnoses),
                final_idf_path=current_idf_path,
                message=f"diagnosed {diagnosis.fault_class} but found no repair candidate",
            )
        current_idf_path = output_dir / f"patched_attempt_{attempt}.idf"
        save_idf(idf, current_idf_path)

    # Unreachable: the loop always returns by the max_attempts-th iteration.
    raise AssertionError("self-heal loop exited without returning")
