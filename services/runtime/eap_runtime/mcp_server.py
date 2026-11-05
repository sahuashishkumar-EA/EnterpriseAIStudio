"""Minimal MCP server (streamable HTTP, JSON responses) exposing published agents as tools.

POST /mcp  JSON-RPC 2.0. Supports the session-less flow of protocol 2026-07-28 (no
initialize required; version read from the MCP-Protocol-Version header or
params._meta) and the classic initialize handshake of 2025-11-25 / 2025-06-18 /
2025-03-26. The server is stateless and does not issue Mcp-Session-Id.
GET  /mcp  405 (no server-initiated stream).

Agents appear when their definition has exposure.mcp = true and the configured alias
(default "prod") points at them. tools/call invokes the agent on behalf of the
authenticated MCP client user.
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SUPPORTED = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]
META_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO = {"name": "eap-runtime", "version": "0.1.0"}

Invoke = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]  # (agent, args) -> InvokeResponse dict
ListAgents = Callable[[], Awaitable[list[dict[str, Any]]]]


def tool_name(agent: str) -> str:
    return agent  # agent names already satisfy MCP tool-name rules ([a-z0-9-])


def _err(id_: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}, status_code=status)


def _ok(id_: Any, result: dict[str, Any], version: str | None = None) -> JSONResponse:
    headers = {"MCP-Protocol-Version": version} if version else None
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result}, headers=headers)


class McpServer:
    def __init__(self, list_agents: ListAgents, allowed_origins: list[str], cache_ttl_s: float = 30):
        self.list_agents = list_agents
        self.allowed_origins = allowed_origins
        self.ttl = cache_ttl_s
        self._tools: tuple[float, list[dict[str, Any]]] | None = None

    def invalidate(self) -> None:
        self._tools = None

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools and time.monotonic() - self._tools[0] < self.ttl:
            return self._tools[1]
        out = []
        for a in await self.list_agents():
            out.append({
                "name": tool_name(a["name"]),
                "title": a["name"],
                "description": a.get("description") or f"Agent {a['name']}",
                "inputSchema": a.get("input_schema") or {"type": "object"},
            })
        self._tools = (time.monotonic(), out)
        return out

    async def handle(self, request: Request, invoke: Invoke | None) -> Response:
        """`invoke` is bound to the authenticated caller; None means unauthenticated."""
        if request.method == "GET":
            return Response(status_code=405, headers={"Allow": "POST"})
        origin = request.headers.get("origin")
        if origin and origin not in self.allowed_origins:
            return _err(None, -32600, "origin not allowed", 403)
        if invoke is None:
            return _err(None, -32001, "unauthorized", 401)
        try:
            msg = await request.json()
        except json.JSONDecodeError:
            return _err(None, -32700, "parse error", 400)
        if isinstance(msg, list):
            return _err(None, -32600, "batching is not supported", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request", 400)
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if mid is None:  # notification (e.g. notifications/initialized)
            return Response(status_code=202)
        header_version = request.headers.get("mcp-protocol-version")
        meta_version = (params.get("_meta") or {}).get(META_KEY)
        version = meta_version or header_version
        if version and version not in SUPPORTED and method != "initialize":
            return _err(mid, -32602, f"unsupported protocol version {version}; supported: {SUPPORTED}", 400)
        if method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = requested if requested in SUPPORTED else SUPPORTED[1]
            return _ok(mid, {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": "Each tool invokes a governed enterprise agent on your behalf.",
            }, negotiated)
        if method == "ping":
            return _ok(mid, {}, version)
        if method == "tools/list":
            return _ok(mid, {"tools": await self.tools()}, version)
        if method == "tools/call":
            name = params.get("name")
            names = {t["name"]: t for t in await self.tools()}
            if name not in names:
                return _err(mid, -32602, f"unknown tool {name}")
            args = params.get("arguments") or {}
            res = await invoke(name, args)
            output = res.get("output")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            if res.get("status") == "failed":
                text = (res.get("error") or {}).get("message", "agent failed")
            result: dict[str, Any] = {
                "content": [{"type": "text", "text": text or ""}],
                "isError": res.get("status") == "failed",
                "_meta": {"eap/run_id": res.get("run_id"), "eap/status": res.get("status"),
                          "eap/cost_usd": (res.get("cost") or {}).get("usd")},
            }
            if isinstance(output, dict) and res.get("status") != "failed":
                result["structuredContent"] = output
            for s in res.get("sources") or []:
                if s.get("uri"):
                    result["content"].append({"type": "resource_link", "uri": s["uri"], "name": s.get("title") or s["id"]})
            return _ok(mid, result, version)
        return _err(mid, -32601, f"method not found: {method}")
