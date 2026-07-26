"""Assemble the MCP server: bind every tool function to a live ServerState.

Every function in ``tools_observe.py``, ``tools_actuate.py`` and
``tools_introspect.py`` takes :class:`~ecoloop.mcp.state.ServerState` as its
first parameter — a plain function is far easier to unit test than a bound
method or a closure captured at definition time. This module is where that
first parameter gets bound via :func:`functools.partial`, which is what lets
the *rest* of each function's signature become the tool's JSON schema:
``inspect.signature`` on a partial correctly reports only the unbound
parameters, so the LLM never sees a ``state`` argument to (mis)supply.

This is also the one place every tool call passes through on its way out —
each registration wraps its function so a raised exception is traced and
turned into a plain-text error result rather than crossing the MCP
transport as a protocol-level failure, and every call gets one line in
:class:`~ecoloop.mcp.trace.TraceWriter` regardless of which module defined it
(``tools_actuate.py`` additionally records its own, more detailed entry on
success, since a published policy's zones and reasoning are worth more than
this module's generic summary — this wrapper skips the generic one for
those two tools to avoid a duplicate, less useful line).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from ecoloop.logging import get_logger
from ecoloop.mcp import tools_actuate, tools_introspect, tools_observe
from ecoloop.mcp.state import ServerState

__all__ = ["build_server"]

_logger = get_logger(__name__, component="mcp")

# (tool function, one-line description). The description is kept here
# explicitly, rather than parsed from each function's docstring at runtime,
# so what the LLM sees can never silently drift from what a reader of this
# file expects it to see.
_OBSERVE_TOOLS: tuple[tuple[Callable[..., Any], str], ...] = (
    (
        tools_observe.get_zone_telemetry,
        "Current temperature, humidity, setpoints and comfort for one or all zones.",
    ),
    (
        tools_observe.get_site_conditions,
        "Current outdoor temperature, humidity, radiation and wind.",
    ),
    (
        tools_observe.get_comfort_status,
        "Building-wide ASHRAE 55 compliance and the worst comfort offender.",
    ),
    (tools_observe.get_energy_totals, "Meter totals over a trailing window, converted to kWh."),
    (
        tools_observe.get_carbon_intensity,
        "Grid carbon intensity for the current hour or an hour ahead.",
    ),
    (tools_observe.get_tariff, "Time-of-use energy price for the current hour or an hour ahead."),
    (
        tools_observe.get_weather_forecast,
        "Read-ahead weather forecast (disclosed oracle) for upcoming hours.",
    ),
    (
        tools_observe.get_demand_status,
        "Rolling electrical demand against the configured peak-shaving cap.",
    ),
)

_ACTUATE_TOOLS: tuple[tuple[Callable[..., Any], str], ...] = (
    (
        tools_actuate.propose_policy,
        "Publish a multi-zone control policy for the reflex tier to clamp and actuate.",
    ),
    (
        tools_actuate.request_zone_setpoint,
        "Propose a heating/cooling setpoint change for a single zone.",
    ),
)

_INTROSPECT_TOOLS: tuple[tuple[Callable[..., Any], str], ...] = (
    (
        tools_introspect.get_active_policy,
        "The currently active control policy, if one exists and has not expired.",
    ),
    (tools_introspect.get_guardrail_violations, "Recent guardrail interventions, newest first."),
    (
        tools_introspect.get_run_manifest,
        "Run-level metadata: profile, controller mode, telemetry health.",
    ),
    (
        tools_introspect.get_run_period_info,
        "One-line description of the active run period and profile.",
    ),
    (
        tools_introspect.get_recent_errors,
        "Recent EnergyPlus .err records at or above a given severity.",
    ),
    (tools_introspect.list_sandboxed_files, "List files under an allowlisted sandbox root."),
    (
        tools_introspect.read_sandboxed_text_file,
        "Read a text file from an allowlisted sandbox root.",
    ),
)

_ACTUATE_FUNCTIONS = frozenset(fn for fn, _ in _ACTUATE_TOOLS)


def build_server(state: ServerState) -> FastMCP:
    """Construct a FastMCP server with every tool bound to ``state``.

    Args:
        state: The live server state every tool reads from or writes to.

    Returns:
        A ready-to-run ``FastMCP`` instance. Call ``.run(transport=...)`` on
        it to serve.
    """
    server = FastMCP(name=state.settings.mcp.server_name)

    for tool_fn, description in (*_OBSERVE_TOOLS, *_ACTUATE_TOOLS, *_INTROSPECT_TOOLS):
        _register(server, state, tool_fn, description)

    return server


def _register(
    server: FastMCP, state: ServerState, tool_fn: Callable[..., Any], description: str
) -> None:
    """Bind one tool function to ``state`` and register it on ``server``.

    Args:
        server: The FastMCP server to register against.
        state: The live server state to bind as the function's first argument.
        tool_fn: The tool function, taking ``state`` as its first parameter.
        description: The tool's one-line description for the LLM.
    """
    bound = functools.partial(tool_fn, state)
    name = tool_fn.__name__
    records_own_trace = tool_fn in _ACTUATE_FUNCTIONS

    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            result = bound(*args, **kwargs)
        except Exception as exc:
            _logger.exception("MCP tool call raised", tool=name)
            _trace(state, name, kwargs, f"raised: {exc}", error=str(exc))
            return f"error: tool '{name}' failed: {exc}"

        if not records_own_trace:
            _trace(state, name, kwargs, f"returned {type(result).__name__}")
        return result

    # inspect.signature(fn) is what FastMCP builds the tool's JSON schema
    # from. A plain *args/**kwargs wrapper has no useful signature of its
    # own, so it is overridden with `bound`'s — which, because it is a
    # functools.partial with `state` already applied, correctly reports only
    # the parameters the LLM actually supplies. eval_str=True is required:
    # every module here uses `from __future__ import annotations`, so the
    # unresolved signature carries bare strings ("ZoneTelemetryResult") rather
    # than the actual classes, and FastMCP's dynamic pydantic model builder
    # cannot resolve a string it has no module context to look the name up in.
    guarded.__name__ = name
    guarded.__doc__ = description
    guarded.__signature__ = inspect.signature(bound, eval_str=True)  # type: ignore[attr-defined]

    server.add_tool(guarded, name=name, description=description)


def _trace(
    state: ServerState,
    tool: str,
    arguments: dict[str, Any],
    result_summary: str,
    *,
    error: str | None = None,
) -> None:
    """Record one generic trace entry for a tool call.

    Args:
        state: Server state, for the trace writer and current sim clock.
        tool: The tool name.
        arguments: The keyword arguments it was called with.
        result_summary: Short description of the outcome.
        error: The error message, if the call failed.
    """
    if state.trace is None:
        return
    sample = state.telemetry.latest()
    state.trace.record(
        tool=tool,
        arguments=arguments,
        result_summary=result_summary,
        sim_clock=sample.clock if sample is not None else None,
        error=error,
    )
