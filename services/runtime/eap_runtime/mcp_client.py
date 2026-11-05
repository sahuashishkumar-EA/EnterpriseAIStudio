"""Minimal MCP client over streamable HTTP (JSON-RPC 2.0).

Protocol versions: the client first tries the stateless 2026-07-28 revision (no initialize
handshake; version sent per request in the MCP-Protocol-Version header and in
params._meta["io.modelcontextprotocol/protocolVersion"]). If the server rejects that, it
falls back to the session-based flow (initialize -> notifications/initialized, then
Mcp-Session-Id on every request) with 2025-11-25. Responses may be plain JSON or an SSE
stream; both are handled.

Re-verify against the current MCP specification before relying on newer features.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx

STATELESS_VERSION = "2026-07-28"
SESSION_VERSION = "2025-11-25"
META_KEY = "io.modelcontextprotocol/protocolVersion"


class McpError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _parse(resp: httpx.Response, want_id: int) -> dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        data_lines: list[str] = []
        for line in resp.text.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "" and data_lines:
                try:
                    msg = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    msg = None
                data_lines = []
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
        raise McpError("no JSON-RPC response in event stream")
    return resp.json()


class McpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None, client: httpx.AsyncClient | None = None,
                 timeout_s: float = 30):
        self.url = url
        self.headers = headers or {}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._ids = itertools.count(1)
        self.mode: str | None = None  # "stateless" | "session"
        self.session_id: str | None = None
        self.version: str = STATELESS_VERSION

    async def _post(self, method: str, params: dict[str, Any] | None, *, notify: bool = False) -> dict[str, Any] | None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        rid = None
        if not notify:
            rid = next(self._ids)
            msg["id"] = rid
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.version,
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        r = await self._client.post(self.url, json=msg, headers=headers)
        if notify:
            return None
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}: {r.text[:300]}", code=r.status_code)
        if r.headers.get("mcp-session-id"):
            self.session_id = r.headers["mcp-session-id"]
        data = _parse(r, rid)
        if "error" in data:
            raise McpError(data["error"].get("message", "error"), data["error"].get("code"))
        return data.get("result", {})

    async def _initialize(self) -> None:
        self.version = SESSION_VERSION
        self.session_id = None
        res = await self._post(
            "initialize",
            {"protocolVersion": SESSION_VERSION, "capabilities": {},
             "clientInfo": {"name": "eap-runtime", "version": "0.1.0"}},
        )
        self.version = (res or {}).get("protocolVersion", SESSION_VERSION)
        await self._post("notifications/initialized", None, notify=True)
        self.mode = "session"

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.mode is None or self.mode == "stateless":
            try:
                p = {**params, "_meta": {**(params.get("_meta") or {}), META_KEY: STATELESS_VERSION}}
                self.version = STATELESS_VERSION
                res = await self._post(method, p)
                self.mode = "stateless"
                return res or {}
            except McpError as e:
                if self.mode == "stateless":
                    raise
                # older server (session required / version unsupported): fall back
                if e.code not in (400, 404, 405, 406, -32600, -32601, -32602, -32000, -32001):
                    raise
            await self._initialize()
        return await self._post(method, params) or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {})).get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments})


def result_payload(result: dict[str, Any]) -> tuple[Any, bool]:
    """(value, is_error) from a tools/call result."""
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"], bool(result.get("isError"))
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(texts), bool(result.get("isError"))
