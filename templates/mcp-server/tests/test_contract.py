"""Contract-test harness shipped with the template (C12-F6). Domain teams keep these and add their own.

1. every registered tool has a tool definition that validates against the C01 tool_def schema
2. the MCP tool schema accepts what the tool definition accepts
3. thresholds are enforced server-side, even if the agent's instructions say otherwise
"""

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from eap_mcp_kit import Caller, ResolvedThresholds, ThresholdSource, caller  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import server as template  # noqa: E402


def fixed(values):
    return ThresholdSource(lambda c: ResolvedThresholds(values, {"profile": "v_test"}))


def test_tool_defs_validate_against_metamodel():
    import eap_metamodel

    mm = eap_metamodel.load()
    for name, tdef in template.TOOL_DEFS.items():
        assert mm.validate("tool_def", tdef) == [], name


def test_mcp_schema_matches_tool_def():
    s = template.build(fixed({"maxExposureEur": 1}))
    tools = asyncio.run(s.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == set(template.TOOL_DEFS)
    t = next(t for t in tools if t.name == "check_exposure")
    assert set(t.input_schema["properties"]) == set(template.TOOL_DEFS["check_exposure"]["inputSchema"]["properties"])


def _call(s, args, c=Caller("customer-profile", "risk")):
    async def go():
        with caller(c):
            return await s.mcp.call_tool("check_exposure", args)

    return asyncio.run(go())


def _is_blocked(result) -> bool:
    return bool(getattr(result, "isError", False)) and "blocked" in json.dumps(
        [getattr(c, "text", "") for c in result.content])


def test_threshold_blocks_server_side():
    s = template.build(fixed({"maxExposureEur": 250000}))
    try:
        r = _call(s, {"customer_id": "C-1001", "amount_eur": 300000})
        assert _is_blocked(r)
    except ToolError as e:
        assert "blocked" in str(e)
    assert s.blocked and s.blocked[0]["limit"] == 250000


def test_within_threshold_passes():
    s = template.build(fixed({"maxExposureEur": 250000}))
    r = _call(s, {"customer_id": "C-1001", "amount_eur": 1000})
    assert not getattr(r, "isError", False)


def test_missing_threshold_fails_closed():
    s = template.build(fixed({}))
    try:
        r = _call(s, {"customer_id": "C-1001", "amount_eur": 1})
        assert _is_blocked(r)
    except ToolError as e:
        assert "blocked" in str(e)
