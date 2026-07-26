"""Tests for MCP server assembly: the tool count, schemas, and the state-binding trick.

The most important thing this file proves is that ``functools.partial`` plus
an explicit ``__signature__`` override actually produces a schema with no
``state`` parameter — get this wrong and every tool call from a real LLM
client fails, but a naive unit test calling the underlying function directly
would never catch it.
"""

from __future__ import annotations

import asyncio

from _mcp_state_factory import make_state

from ecoloop.mcp.server import _ACTUATE_TOOLS, _INTROSPECT_TOOLS, _OBSERVE_TOOLS, build_server


def test_exactly_seventeen_tools_are_registered() -> None:
    total = len(_OBSERVE_TOOLS) + len(_ACTUATE_TOOLS) + len(_INTROSPECT_TOOLS)
    assert total == 17


def test_server_registers_every_declared_tool() -> None:
    server = build_server(make_state())
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 17


def test_no_tool_schema_exposes_the_state_parameter() -> None:
    """If this ever fails, a real LLM client would see a `state` argument it
    has no way to fill in - the exact bug the eval_str=True fix addressed."""
    server = build_server(make_state())
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        assert "state" not in properties, f"{tool.name} leaked its state parameter"


def test_tool_names_match_the_underlying_functions() -> None:
    server = build_server(make_state())
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    expected = {fn.__name__ for fn, _ in (*_OBSERVE_TOOLS, *_ACTUATE_TOOLS, *_INTROSPECT_TOOLS)}
    assert names == expected


def test_calling_a_tool_returns_the_expected_shape() -> None:
    server = build_server(make_state())
    _content, structured = asyncio.run(server.call_tool("get_run_manifest", {}))
    assert structured["profile"] == "fast"


def test_calling_an_actuate_tool_with_no_simulation_is_refused_not_raised() -> None:
    server = build_server(make_state())
    _content, structured = asyncio.run(
        server.call_tool(
            "propose_policy",
            {
                "zone_setpoints": [
                    {"zone": "CORE_ZN", "heating_setpoint_c": 21.0, "cooling_setpoint_c": 24.0}
                ],
                "reasoning": "test",
            },
        )
    )
    assert structured["accepted"] is False


def test_sandbox_escape_through_the_full_server_is_blocked() -> None:
    server = build_server(make_state())
    _content, structured = asyncio.run(
        server.call_tool("read_sandboxed_text_file", {"path": "../../../../etc/passwd"})
    )
    assert structured["result"].startswith("error:")
