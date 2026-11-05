import asyncio
import json

import httpx
import pytest

from eap_core.context import CallContext
from eap_runtime.mcp_client import McpClient, McpError, result_payload
from eap_runtime.tools import ToolRunner
from examples.seed import seed
from tests.harness import build

USER = {"X-Dev-User": "dave@example.com", "X-Dev-Roles": "retail:viewer"}


@pytest.fixture
def plat():
    p = build()

    async def go():
        async with p.gov_client() as g, p.rt_async_client() as r:
            await seed(g, r)

    asyncio.run(go())
    return p


def rpc(c, method, params=None, id_=1, headers=None, version=None):
    h = {**USER, "Accept": "application/json, text/event-stream"}
    if version:
        h["MCP-Protocol-Version"] = version
    h.update(headers or {})
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", json=body, headers=h)


def test_mcp_server_session_flow(plat):
    with plat.rt_client() as c:
        init = rpc(c, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).json()
        assert init["result"]["protocolVersion"] == "2025-11-25" and "tools" in init["result"]["capabilities"]
        assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=USER).status_code == 202
        tools = rpc(c, "tools/list", {}, 2, version="2025-11-25").json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {"customer-profile", "entity-resolver"}  # evaluator has exposure.mcp = false
        cp = next(t for t in tools if t["name"] == "customer-profile")
        assert cp["inputSchema"]["required"] == ["customer_id"]
        res = rpc(c, "tools/call", {"name": "customer-profile", "arguments": {"customer_id": "C-1001"}}, 3,
                  version="2025-11-25").json()["result"]
        assert res["isError"] is False and res["structuredContent"]["name"] == "Anna de Vries"
        assert json.loads(res["content"][0]["text"])["segment"] == "premium"
        assert res["_meta"]["eap/status"] == "ok"
        assert any(x["type"] == "resource_link" for x in res["content"])  # sources propagated from the child agent


def test_mcp_server_stateless_and_errors(plat):
    with plat.rt_client() as c:
        meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
        r = rpc(c, "tools/list", meta, version="2026-07-28")
        assert r.status_code == 200 and r.headers["mcp-protocol-version"] == "2026-07-28"
        assert len(r.json()["result"]["tools"]) == 2
        assert rpc(c, "tools/call", {"name": "evaluator", "arguments": {}}).json()["error"]["code"] == -32602
        assert rpc(c, "nope", {}).json()["error"]["code"] == -32601
        assert rpc(c, "tools/list", {}, version="1999-01-01").status_code == 400
        assert c.get("/mcp", headers=USER).status_code == 405
        assert rpc(c, "tools/list", {}, headers={"Origin": "https://evil.example"}).status_code == 403
        unauth = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert unauth.status_code == 401
        # caller without a role in the owning BU gets a tool error, not data
        outsider = {"X-Dev-User": "eve@example.com", "X-Dev-Roles": "finance:viewer"}
        res = rpc(c, "tools/call", {"name": "customer-profile", "arguments": {"customer_id": "C-1001"}},
                  headers=outsider).json()["result"]
        assert res["isError"] is True and "may not invoke" in res["content"][0]["text"]


def test_mcp_client_against_runtime_server(plat):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=plat.runtime), base_url="http://runtime") as hc:
            client = McpClient("http://runtime/mcp", headers=USER, client=hc)
            tools = await client.list_tools()
            assert client.mode == "stateless"
            res = await client.call_tool("entity-resolver", {"customer_id": "C-1002"})
            return tools, res

    tools, res = asyncio.run(go())
    assert {t["name"] for t in tools} == {"customer-profile", "entity-resolver"}
    value, is_error = result_payload(res)
    assert not is_error and value["customer_id"] == "C-1002"


class SessionOnlyServer:
    """A 2025-11-25-style server that requires initialize + Mcp-Session-Id and answers via SSE."""

    def __init__(self):
        self.seen = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        msg = json.loads(req.content)
        self.seen.append((msg.get("method"), dict(req.headers)))
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "s", "version": "1"}}},
                headers={"Mcp-Session-Id": "sess-1"})
        if "id" not in msg:
            return httpx.Response(202)
        if req.headers.get("mcp-session-id") != "sess-1":
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32600, "message": "no session"}})
        result = {"content": [{"type": "text", "text": f"hello {msg['params']['arguments'].get('who')}"}], "isError": False}
        sse = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})


def test_mcp_client_falls_back_to_session_flow_and_tool_runner_forwards_identity():
    server = SessionOnlyServer()

    async def go():
        runner = ToolRunner(transport=httpx.MockTransport(server))
        ctx = CallContext.root(on_behalf_of="dave@example.com").enter("a")
        entry = {"id": "greeter", "kind": "mcp", "status": "active",
                 "config": {"url": "https://mcp.internal/mcp", "tool": "greet", "auth": "none"}}
        return await runner.run(entry, {"who": "world"}, ctx)

    assert asyncio.run(go()) == "hello world"
    methods = [m for m, _ in server.seen]
    assert methods == ["tools/call", "initialize", "notifications/initialized", "tools/call"]
    last_headers = server.seen[-1][1]
    assert last_headers["mcp-session-id"] == "sess-1" and last_headers["mcp-protocol-version"] == "2025-11-25"
    assert last_headers["x-on-behalf-of"] == "dave@example.com"


def test_mcp_client_surfaces_errors():
    def handler(req):
        msg = json.loads(req.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "boom"}})

    async def go():
        c = McpClient("https://x/mcp", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await c.call_tool("t", {})

    with pytest.raises(McpError, match="boom"):
        asyncio.run(go())
