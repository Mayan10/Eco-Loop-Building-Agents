"""Parse an EnergyPlus ``.err`` file into typed, severity-tagged records.

The ``.err`` file is EnergyPlus's own error log, distinct from the simulation
stdout. Its format is simple but has one trap: a ``** ~~~ **`` line is a
*continuation* of the record immediately above it, not a new record. Splitting
naively on ``**`` markers therefore over-counts errors and truncates their
messages at the first line break.

**This content is untrusted input.** ``.err`` text is written by a running
simulation and, once self-healing or the cognitive layer inspects it, is placed
in front of an LLM. It is treated here as data, never instruction: control
characters are stripped and the total parsed size is capped
(``simulation.output.max_err_bytes``), so a pathological or truncated file
cannot blow up the prompt budget downstream.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ecoloop.errors import SimulationFatalError
from ecoloop.logging import get_logger

__all__ = [
    "ErrFileSummary",
    "ErrorRecord",
    "Severity",
    "parse_err_file",
]

_logger = get_logger(__name__, component="simulation")

# `   ** Severe  ** <text>` / `   **  Fatal  ** <text>` / `   ** Warning ** <text>`
_RECORD_RE = re.compile(r"^\s*\*\*\s*(Severe|Warning|Fatal)\s*\*\*\s?(.*)$", re.IGNORECASE)
# `   **   ~~~   ** <text>` continues the record immediately above.
_CONTINUATION_RE = re.compile(r"^\s*\*\*\s*~~~\s*\*\*\s?(.*)$")
_SUCCESS_RE = re.compile(r"EnergyPlus Completed Successfully")
_TERMINATED_RE = re.compile(r"EnergyPlus Terminated")
# Trailer: "EnergyPlus Completed Successfully-- 3 Warning; 1 Severe Errors; ..."
_TRAILER_COUNT_RE = re.compile(r"(\d+)\s+(Warning|Severe)", re.IGNORECASE)


class Severity(StrEnum):
    """EnergyPlus ``.err`` severity levels, ordered from least to most serious."""

    INFO = "info"
    WARNING = "warning"
    SEVERE = "severe"
    FATAL = "fatal"


_SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.INFO,
    Severity.WARNING,
    Severity.SEVERE,
    Severity.FATAL,
)


class ErrorRecord(BaseModel):
    """One error, warning, or fatal message from an EnergyPlus run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Severity
    message: str
    line_number: int
    raw: str
    occurrences: int = 1


class ErrFileSummary(BaseModel):
    """The parsed contents of one ``.err`` file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    records: tuple[ErrorRecord, ...]
    counts: dict[Severity, int]
    completed_successfully: bool
    truncated: bool

    @property
    def worst_severity(self) -> Severity | None:
        """The most serious severity present in this file.

        Returns:
            The highest-ranked :class:`Severity` seen, or ``None`` if the file
            contained no error records at all.
        """
        present = [s for s in _SEVERITY_ORDER if self.counts.get(s, 0) > 0]
        return present[-1] if present else None

    def filter(self, severity: Severity) -> tuple[ErrorRecord, ...]:
        """Return only the records at exactly the given severity.

        Args:
            severity: Severity to select.

        Returns:
            Matching records, in file order.
        """
        return tuple(r for r in self.records if r.severity == severity)


def _strip_control_characters(text: str) -> str:
    r"""Remove control characters from untrusted log text, keeping newlines and tabs.

    Args:
        text: Raw text as read from the ``.err`` file.

    Returns:
        Text with every Unicode "Cc" control character removed except ``\n``
        and ``\t``.
    """
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")


def _read_capped(path: Path, max_bytes: int) -> tuple[str, bool]:
    """Read a file, keeping only its head and tail if it exceeds the cap.

    Args:
        path: File to read.
        max_bytes: Maximum bytes to keep in total.

    Returns:
        A ``(text, truncated)`` pair. When truncated, ``text`` is the first and
        last halves of the cap joined by a marker line, which keeps both the
        opening context (model version, IDF echo) and the closing trailer
        (the pass/fail summary line) that downstream code depends on.
    """
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_text(encoding="utf-8", errors="replace"), False

    half = max_bytes // 2
    with path.open("rb") as handle:
        head = handle.read(half).decode("utf-8", errors="replace")
        handle.seek(size - half)
        tail = handle.read(half).decode("utf-8", errors="replace")
    marker = "\n... [ecoloop: middle of .err file omitted, exceeds max_err_bytes] ...\n"
    return head + marker + tail, True


def _finalize(current: dict[str, object] | None, records: list[ErrorRecord]) -> None:
    """Append the in-progress record to the output list, if there is one.

    Args:
        current: Mutable state for the record being assembled, or ``None``.
        records: Output list to append to.
    """
    if current is None:
        return
    records.append(
        ErrorRecord(
            severity=current["severity"],  # type: ignore[arg-type]
            message=str(current["message"]).strip(),
            line_number=current["line_number"],  # type: ignore[arg-type]
            raw=str(current["raw"]),
        )
    )


def parse_err_file(path: Path, *, max_bytes: int) -> ErrFileSummary:
    """Parse an EnergyPlus ``.err`` file.

    Args:
        path: Path to the ``.err`` file.
        max_bytes: Cap on bytes parsed, from ``simulation.output.max_err_bytes``.
            Larger files are read from the head and tail only.

    Returns:
        The parsed, severity-tagged summary.

    Raises:
        SimulationFatalError: If ``path`` does not exist or cannot be read.
    """
    if not path.is_file():
        raise SimulationFatalError("EnergyPlus .err file not found", path=str(path))

    try:
        text, truncated = _read_capped(path, max_bytes)
    except OSError as exc:
        raise SimulationFatalError(
            "could not read EnergyPlus .err file", path=str(path), cause=str(exc)
        ) from exc

    text = _strip_control_characters(text)

    records: list[ErrorRecord] = []
    current: dict[str, object] | None = None
    completed_successfully = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        record_match = _RECORD_RE.match(line)
        continuation_match = _CONTINUATION_RE.match(line)

        if record_match:
            _finalize(current, records)
            severity = Severity(record_match.group(1).lower())
            body = record_match.group(2)
            current = {
                "severity": severity,
                "message": body,
                "line_number": line_number,
                "raw": line,
            }
        elif continuation_match and current is not None:
            current["message"] = f"{current['message']} {continuation_match.group(1)}".strip()
            current["raw"] = f"{current['raw']}\n{line}"
        elif _SUCCESS_RE.search(line):
            completed_successfully = True
        elif _TERMINATED_RE.search(line):
            completed_successfully = False

    _finalize(current, records)

    counts: dict[Severity, int] = dict.fromkeys(_SEVERITY_ORDER, 0)
    for record in records:
        counts[record.severity] += 1

    _logger.info(
        "parsed .err file",
        path=str(path),
        severe=counts[Severity.SEVERE],
        fatal=counts[Severity.FATAL],
        warning=counts[Severity.WARNING],
        completed_successfully=completed_successfully,
        truncated=truncated,
    )

    return ErrFileSummary(
        path=path,
        records=tuple(records),
        counts=counts,
        completed_successfully=completed_successfully,
        truncated=truncated,
    )
