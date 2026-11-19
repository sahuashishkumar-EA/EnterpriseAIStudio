"""C08 Config MCP server: active behaviour profiles for low-code agents (spec 7.3).

    get_active_profile(agent_id, business_unit) -> persona, instructions, output_format,
                                                   thresholds (display only), escalation_contacts, versions

Reads through the resolver (C07), so it inherits its cache and last-known-good. The calling agent
is authenticated by Agent Gateway / the MCP gateway (C08-F3) and arrives in X-EAP-Agent-Id; an
agent may only ask for its own profile. Every call is logged with the versions served.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from eap_metamodel.resolve import hot_view

log = logging.getLogger("eap.config_mcp")

ResolveFn = Callable[[str, str, str], Awaitable[dict[str, Any]]]  # (agent_id, bu, environment) -> resolved


def build(resolve: ResolveFn, calling_agent: Callable[[], str], environment: str | None = None) -> MCPServer:
    env = environment or os.environ.get("EAP_ENVIRONMENT", "production")
    server = MCPServer("eap-config", instructions="Call get_active_profile once at conversation start.")
    served: list[dict[str, Any]] = []
    server.served = served  # type: ignore[attr-defined]

    @server.tool(name="get_active_profile",
                 description="Return the active behavior profile for this agent and business unit. "
                             "Call once at conversation start.")
    async def get_active_profile(agent_id: str, business_unit: str) -> dict:
        caller = calling_agent()
        if caller != agent_id:
            raise ToolError(f"agent {caller} may read only its own profile")
        resolved = await resolve(agent_id, business_unit, env)
        out = hot_view(resolved)  # C08-F2: hot fields only
        served.append({"agent": agent_id, "bu": business_unit, "versions": out["versions"]})
        log.info("served profile", extra={"agent": agent_id, "bu": business_unit, "versions": out["versions"]})
        return out

    return server


__all__ = ["build"]
