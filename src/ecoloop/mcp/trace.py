"""Append-only trace of every MCP tool call, for the "why did you do that" question.

Every tool invocation — successful or not — gets one line in
``agent_trace.jsonl``: the tool name, its arguments, a summary of what it
returned, and the simulation clock at the time. This is what answers
CLAUDE.md's "why did you raise the cooling set-point at 2 pm?" — the
reasoning an agent-sourced ``ControlPolicy`` carries is only useful if there
is a durable record connecting it to the tool call that produced it.

Capped at ``logging.max_trace_mib``: a runaway agent calling tools in a tight
loop hits a wall and stops being traced, not fills the disk. The cap is
checked before every write, not merely at startup, since MCP tool calls
happen throughout a run, not just once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecoloop.bus.models import SimClock
from ecoloop.logging import get_logger

__all__ = ["TraceWriter"]

_logger = get_logger(__name__, component="mcp")


class TraceWriter:
    """Appends one JSON line per MCP tool call to a capped trace file.

    Args:
        path: Destination file, typically ``results/<run_id>/agent_trace.jsonl``.
        max_bytes: Size cap, from ``logging.max_trace_mib * 1024 * 1024``.
    """

    def __init__(self, path: Path, *, max_bytes: int) -> None:
        """Create a trace writer, creating the destination's parent directory."""
        self._path = path
        self._max_bytes = max_bytes
        self._cap_warning_logged = False
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        result_summary: str,
        sim_clock: SimClock | None,
        error: str | None = None,
    ) -> None:
        """Append one trace entry, unless the size cap has been reached.

        Args:
            tool: The MCP tool name.
            arguments: The arguments it was called with. Caller is
                responsible for keeping these small and JSON-serialisable —
                this method does not itself truncate individual fields.
            result_summary: A short, human-readable description of the
                outcome. Not the full result payload — trace entries are for
                explainability, not for replaying tool output verbatim.
            sim_clock: The simulation clock at the time of the call, or
                ``None`` if no simulation is active.
            error: The error message, if the tool call failed.
        """
        if self._at_capacity():
            if not self._cap_warning_logged:
                _logger.warning(
                    "agent trace file reached its size cap; no longer recording",
                    path=str(self._path),
                )
                self._cap_warning_logged = True
            return

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "sim_clock": sim_clock.isoformat() if sim_clock is not None else None,
            "tool": tool,
            "arguments": arguments,
            "result_summary": result_summary,
            "error": error,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

    def _at_capacity(self) -> bool:
        """Whether the trace file has reached its configured size cap."""
        return self._path.exists() and self._path.stat().st_size >= self._max_bytes
