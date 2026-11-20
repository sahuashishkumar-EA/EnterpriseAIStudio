"""Operations: chat feedback and escalations land in the governor's Monitor inbox; runs explorer and stats."""

import asyncio

import httpx
from starlette.testclient import TestClient

from eap_chat.app import Settings as ChatSettings
from eap_chat.app import create_app as chat_app
from eap_core.auth import AuthConfig
from tests.harness import build

ME = {"X-Dev-User": "me@example.com", "X-Dev-Roles": "*:platform_admin"}
OTHER = {"X-Dev-User": "olga@example.com", "X-Dev-Roles": "hr:bu_admin"}
IT_VIEWER = {"X-Dev-User": "vic@example.com", "X-Dev-Roles": "it:viewer"}
USER = {"X-Dev-User": "ann@example.com"}


class _Sync:
    def __init__(self, p):
        self.p = p

    def get(self, path, headers):
        return self.p.gov("GET", path, headers)

    def post(self, path, json, headers):
        return self.p.gov("POST", path, headers, json=json)


def test_feedback_escalations_and_monitor():
    p = build(gov_overrides={"relaxed_release": True})

    async def setup():
        async with p.gov_client() as g:
            assert (await g.post("/api/v1/agents", json={"name": "helpdesk", "owner_bu": "it"}, headers=ME)).status_code == 201
            ctx = {"name": "Employees", "role": "the IT helpdesk for employees",
                   "guardrails": {"escalation": {"triggers": ["urgent"], "message": "Paging the on-call engineer."}}}
            await g.put("/api/v1/agents/helpdesk/draft/contexts/default", json={"context": ctx}, headers=ME)
            q = await g.post("/api/v1/agents/helpdesk/quick-publish", json={}, headers=ME)
            assert q.status_code == 200, q.text
            await g.put("/api/v1/users/ann@example.com/memberships/it", json={"roles": ["member"]}, headers=ME)

    asyncio.run(setup())
    chat = chat_app(ChatSettings(governor_url="http://governor", runtime_url="http://runtime", auth=AuthConfig(modes=["dev"])),
                    governor_transport=httpx.ASGITransport(app=p.governor), runtime_transport=httpx.ASGITransport(app=p.runtime))
    with TestClient(chat) as c:
        ok = c.post("/api/chat", headers=USER, json={"bu": "it", "agent": "helpdesk", "message": "My laptop is slow"}).json()
        esc = c.post("/api/chat", headers=USER, json={"bu": "it", "agent": "helpdesk", "message": "this is urgent"}).json()
        bad = c.post("/api/chat", headers=USER, json={"bu": "it", "agent": "helpdesk", "message": "VPN keeps dropping"}).json()
        assert c.post("/api/feedback", headers=USER, json={"run_id": ok["run_id"], "rating": "up"}).status_code == 200
        assert c.post("/api/feedback", headers=USER, json={"run_id": bad["run_id"], "rating": "down",
                                                            "comment": "did not mention the VPN client"}).status_code == 200
        assert c.post("/api/feedback", headers=USER, json={"run_id": bad["run_id"], "rating": "meh"}).status_code == 400
        # another user cannot rate ann's answer
        assert c.post("/api/feedback", headers={"X-Dev-User": "bob@example.com"},
                      json={"run_id": ok["run_id"], "rating": "down"}).status_code == 404

    g = _Sync(p)
    inbox = g.get("/api/v1/ops/runs?review=open", headers=ME).json()["runs"]
    assert {r["run_id"] for r in inbox} == {esc["run_id"], bad["run_id"]}
    e = next(r for r in inbox if r["run_id"] == esc["run_id"])
    assert "escalated" in e["flags"] and e["context_id"] == "default" and e["input_text"] == "this is urgent"
    assert next(r for r in inbox if r["run_id"] == bad["run_id"])["feedback"]["comment"] == "did not mention the VPN client"
    # only failures / filters
    assert [r["run_id"] for r in g.get("/api/v1/ops/runs?feedback=up", headers=ME).json()["runs"]] == [ok["run_id"]]
    # run detail with timeline
    d = g.get(f"/api/v1/ops/runs/{bad['run_id']}", headers=ME).json()
    assert d["steps"] and d["input"] == {"message": "VPN keeps dropping"} and d["can_review"]
    # BU scoping: hr admin sees nothing from it and cannot open the run; an it viewer can read but not review
    assert g.get("/api/v1/ops/runs", headers=OTHER).json()["runs"] == []
    assert g.get(f"/api/v1/ops/runs/{bad['run_id']}", headers=OTHER).status_code == 403
    assert len(g.get("/api/v1/ops/runs", headers=IT_VIEWER).json()["runs"]) == 3
    assert g.get(f"/api/v1/ops/runs/{bad['run_id']}", headers=IT_VIEWER).json()["can_review"] is False
    assert g.post(f"/api/v1/ops/runs/{bad['run_id']}/review", json={"status": "resolved"}, headers=IT_VIEWER).status_code == 403
    # assign to me, then resolve with a note
    a = g.post(f"/api/v1/ops/runs/{esc['run_id']}/review", json={"assignee": "me"}, headers=ME).json()
    assert a["assignee"] == "me@example.com" and a["review_status"] == "open"
    r = g.post(f"/api/v1/ops/runs/{esc['run_id']}/review", json={"status": "resolved", "note": "called back"}, headers=ME).json()
    assert r["review_status"] == "resolved" and r["review_note"] == "called back" and r["reviewed_by"] == "me@example.com"
    assert [x["run_id"] for x in g.get("/api/v1/ops/runs?review=open", headers=ME).json()["runs"]] == [bad["run_id"]]
    # dashboard numbers
    s = g.get("/api/v1/ops/stats?days=7", headers=ME).json()
    h = next(a for a in s["agents"] if a["agent"] == "helpdesk")
    assert (h["runs"], h["escalated"], h["up"], h["down"], h["open"]) == (3, 1, 1, 1, 1)
    assert len(h["days"]) == 7 and h["days"][-1]["runs"] == 3 and h["p95_ms"] >= h["p50_ms"]
    assert g.get("/api/v1/ops/stats", headers=OTHER).json()["agents"] == []
    audit = g.get("/api/v1/audit?target=agent/helpdesk", headers=ME).json()
    assert any(x["action"] == "run.review" for x in audit["events"])
