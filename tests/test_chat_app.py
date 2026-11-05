"""The separate chat app talks to published agents through the runtime on behalf of the user."""

import asyncio

import httpx
from starlette.testclient import TestClient

from eap_chat.app import Settings as ChatSettings
from eap_chat.app import create_app as chat_app
from eap_core.auth import AuthConfig
from examples.seed import seed
from tests.harness import build

ME = {"X-Dev-User": "me@example.com", "X-Dev-Roles": "*:platform_admin"}
USER = {"X-Dev-User": "ann@example.com"}


def test_chat_app_end_to_end():
    p = build(gov_overrides={"relaxed_release": True})

    async def go():
        async with p.gov_client() as g, p.rt_async_client() as r:
            await seed(g, r)  # customer-profile etc. (not chat-enabled)
            assert (await g.post("/api/v1/agents", json={"name": "helpdesk", "owner_bu": "it"}, headers=ME)).status_code == 201
            ctx = {"name": "Employees", "role": "the IT helpdesk", "starters": ["How do I reset my password?"],
                   "guardrails": {"escalation": {"triggers": ["urgent"], "message": "Paging the on-call engineer."}}}
            await g.put("/api/v1/agents/helpdesk/draft/contexts/staff", json={"context": ctx}, headers=ME)
            await g.put("/api/v1/agents/helpdesk/draft/contexts/visitors", json={"context": {"name": "Visitors", "role": "a reception desk"}}, headers=ME)
            await g.delete("/api/v1/agents/helpdesk/draft/contexts/default", headers=ME)
            q = await g.post("/api/v1/agents/helpdesk/quick-publish", json={}, headers=ME)
            assert q.status_code == 200, q.text

    asyncio.run(go())
    app = chat_app(ChatSettings(governor_url="http://governor", runtime_url="http://runtime", auth=AuthConfig(modes=["dev"])),
                   governor_transport=httpx.ASGITransport(app=p.governor), runtime_transport=httpx.ASGITransport(app=p.runtime))
    with TestClient(app) as c:
        assert c.get("/").status_code == 200 and "Agent Chat" in c.get("/").text
        assert c.get("/api/me").status_code == 401
        agents = c.get("/api/agents", headers=USER).json()["agents"]
        assert [a["name"] for a in agents] == ["helpdesk"]  # only chat-enabled agents
        assert {x["id"] for x in agents[0]["contexts"]} == {"staff", "visitors"}
        r = c.post("/api/chat", headers=USER, json={"agent": "helpdesk", "context": "staff", "message": "My laptop is slow",
                                                    "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello"}]}).json()
        assert r["status"] == "ok" and r["context"] == "staff" and r["version"] == "1.0.0" and r["reply"]
        esc = c.post("/api/chat", headers=USER, json={"agent": "helpdesk", "context": "staff", "message": "this is urgent"}).json()
        assert esc["reply"] == "Paging the on-call engineer." and "escalated" in esc["flags"]
        v = c.post("/api/chat", headers=USER, json={"agent": "helpdesk", "context": "visitors", "message": "this is urgent"}).json()
        assert v["context"] == "visitors" and v["flags"] == []
        # agents not enabled for chat cannot be reached through the chat app
        no = c.post("/api/chat", headers=USER, json={"agent": "customer-profile", "message": "C-1001"})
        assert no.status_code == 403
        # the run is recorded on behalf of the end user, not the chat service
        run = asyncio.run(p.runtime.state.svc.runs.get(r["run_id"]))
        assert run["on_behalf_of"] == "ann@example.com"


def test_only_delegating_services_may_act_on_behalf():
    p = build()
    with p.rt_client() as c:
        r = c.post("/v1/agents/x@prod/invoke", json={"input": {}},
                   headers={"X-Dev-User": "mallory@example.com", "X-EAP-On-Behalf-Of": "ceo@example.com"})
        assert r.status_code == 403
