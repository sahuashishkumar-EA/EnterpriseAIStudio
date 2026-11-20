"""Agents built and run elsewhere (Copilot Studio, any HTTP agent) stay governed by the governor."""

import json

import httpx
import pytest

from eap_governor import external
from tests.harness import build

ADMIN = {"X-Dev-User": "me@example.com", "X-Dev-Roles": "*:platform_admin"}
CONNECTOR = {"X-Dev-User": "copilot-connector", "X-Dev-Kind": "service"}
STRANGER = {"X-Dev-User": "other-app", "X-Dev-Kind": "service"}

CTX = {"name": "Employees", "role": "the HR assistant for employees of the bank",
       "guardrails": {"blocked_topics": ["salary of colleagues"], "refusal_message": "I can't discuss colleagues' pay.",
                      "escalation": {"triggers": ["harassment"], "message": "I'm connecting you with an HR advisor."}}}


class FakeWorld:
    """Direct Line (Copilot Studio), a plain HTTP agent and a webhook receiver."""

    def __init__(self):
        self.hooks: list[httpx.Request] = []
        self.sent: dict[str, str] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.startswith("https://hooks.example.com/"):
            self.hooks.append(req)
            return httpx.Response(204)
        if u.startswith("https://agent.example.com/"):
            body = json.loads(req.content)
            assert req.headers["authorization"] == "Bearer http-token"
            return httpx.Response(200, json={"output": f"HTTP agent: {body['message']} [{body['context']}]"})
        dl = "https://directline.botframework.com/v3/directline"
        assert req.headers["authorization"] in ("Bearer dl-secret", "Bearer conv-token"), req.headers
        if req.method == "POST" and u == f"{dl}/conversations":
            return httpx.Response(201, json={"conversationId": "c1", "token": "conv-token"})
        if req.method == "POST" and u.endswith("/conversations/c1/activities"):
            a = json.loads(req.content)
            self.sent["c1|1"] = a["text"]
            assert a["channelData"]["eap_context"] == "staff"
            return httpx.Response(200, json={"id": "c1|1"})
        if req.method == "GET" and "/conversations/c1/activities" in u:
            acts = [{"type": "typing", "from": {"id": "bot"}, "replyToId": "c1|1"},
                    {"type": "message", "from": {"id": "bot"}, "replyToId": "c1|1",
                     "text": f"Copilot: you can request leave in Workday ({self.sent.get('c1|1')})"}]
            return httpx.Response(200, json={"activities": acts, "watermark": "2"})
        return httpx.Response(404)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("DL_SECRET", "dl-secret")
    monkeypatch.setenv("HOOK_SECRET", "hook-secret")
    monkeypatch.setenv("HTTP_AGENT_TOKEN", "http-token")
    monkeypatch.setattr(external, "POLL_S", 0.01)
    monkeypatch.setattr(external, "QUIET_S", 0.05)
    return FakeWorld()


def ok(r: httpx.Response, status: int = 200):
    assert r.status_code == status, r.text
    return r.json()


def make_agent(p, name, hosting):
    ok(p.gov("POST", "/api/v1/agents", ADMIN, json={"name": name, "owner_bu": "hr"}), 201)
    ok(p.gov("PUT", f"/api/v1/agents/{name}/draft/contexts/staff", ADMIN, json={"context": CTX}))
    ok(p.gov("PUT", f"/api/v1/agents/{name}/draft/default-context", ADMIN, json={"context": "staff"}))
    ok(p.gov("DELETE", f"/api/v1/agents/{name}/draft/contexts/default", ADMIN))
    d = ok(p.gov("GET", f"/api/v1/agents/{name}", ADMIN))["draft"]
    ok(p.gov("PUT", f"/api/v1/agents/{name}/draft/technical", ADMIN,
             json={"definition": {**d["definition"], "hosting": hosting}, "base": d["sections"]["technical"]}))


def test_copilot_studio_agent_is_governed(world):
    p = build(gov_overrides={"relaxed_release": True, "edge_url": "https://edge.example.com"},
              external_transport=httpx.MockTransport(world.handler))
    make_agent(p, "hr-copilot", {"platform": "copilot_studio", "auth_secret": "DL_SECRET", "callers": ["copilot-connector"],
                                 "webhook_url": "https://hooks.example.com/eap", "webhook_secret": "HOOK_SECRET"})

    # 1. release gate tests the real external agent (through Direct Line), not a simulation
    ok(p.gov("POST", "/api/v1/agents/hr-copilot/evals/suites", ADMIN, json={
        "name": "smoke", "required": True, "cases": [{"id": "leave", "context": "staff", "input": {"message": "How do I book leave?"},
                                                      "assertions": [{"type": "contains", "path": "output", "value": "Workday"}]}]}), 201)
    run = ok(p.gov("POST", "/api/v1/agents/hr-copilot/evals/run?wait=true", ADMIN, json={}))["runs"][0]
    assert run["status"] == "completed" and run["passed"] == 1, run
    assert "Copilot: you can request leave" in run["results"][0]["output"]

    # 2. going live notifies the external platform with a signed webhook
    ok(p.gov("POST", "/api/v1/agents/hr-copilot/quick-publish", ADMIN, json={}))
    assert len(world.hooks) == 1
    h = world.hooks[0]
    ev = json.loads(h.content)
    assert ev["event"] == "agent.version_live" and ev["version"] == "1.0.0"
    assert ev["profile_url"] == "https://edge.example.com/gov/v1/agents/hr-copilot/profile"
    assert external.verify("hook-secret", h.headers["x-eap-timestamp"], h.content, h.headers["x-eap-signature"])
    assert not external.verify("wrong", h.headers["x-eap-timestamp"], h.content, h.headers["x-eap-signature"])

    with p.rt_client() as rt:
        # 3. the external agent pulls its governed instructions (ETag -> 304 until the next version)
        prof = rt.get("/gov/v1/agents/hr-copilot/profile?context=staff", headers=CONNECTOR)
        body = ok(prof)
        assert body["version"] == "1.0.0" and body["platform"] == "copilot_studio" and body["context"]["id"] == "staff"
        assert "HR assistant for employees" in body["instructions"] and "Platform rules" in body["instructions"]
        assert {g["type"] for g in body["guardrails"]} >= {"topic_policy", "escalation"}
        assert rt.get("/gov/v1/agents/hr-copilot/profile?context=staff",
                      headers={**CONNECTOR, "If-None-Match": prof.headers["etag"]}).status_code == 304
        assert rt.get("/gov/v1/agents/hr-copilot/profile", headers=STRANGER).status_code == 403

        # 4. hard controls around every turn: checks decide, the platform only relays
        esc = ok(rt.post("/gov/v1/agents/hr-copilot/check/input", headers=CONNECTOR,
                         json={"message": "I want to report harassment", "context": "staff", "user": "eve@example.com"}))
        assert esc["decision"] == "respond" and esc["message"] == "I'm connecting you with an HR advisor."
        assert esc["flags"] == ["escalated"]
        off = ok(rt.post("/gov/v1/agents/hr-copilot/check/input", headers=CONNECTOR,
                         json={"message": "What is the salary of colleagues in my team?", "context": "staff"}))
        assert off["decision"] == "respond" and "colleagues' pay" in off["message"]
        fine = ok(rt.post("/gov/v1/agents/hr-copilot/check/input", headers=CONNECTOR,
                          json={"message": "How many leave days do I have?", "context": "staff", "user": "eve@example.com"}))
        assert fine["decision"] == "allow" and fine["text"] == "How many leave days do I have?"
        red = ok(rt.post("/gov/v1/agents/hr-copilot/check/input", headers=CONNECTOR,
                         json={"message": "mail me at eve@example.com", "context": "staff"}))
        assert red["decision"] == "allow" and "eve@example.com" not in red["text"]
        out = ok(rt.post("/gov/v1/agents/hr-copilot/check/output", headers=CONNECTOR,
                         json={"check_id": fine["check_id"], "output": "You have 12 days left.", "context": "staff"}))
        assert out["decision"] == "allow" and out["check_id"] == fine["check_id"]
        assert ok(rt.post("/gov/v1/agents/hr-copilot/runs", headers=CONNECTOR, json={
            "message": "Where is the handbook?", "output": "On the intranet.", "context": "staff", "user": "eve@example.com",
            "latency_ms": 900, "status": "ok"}), 201)["recorded"]
        assert rt.post("/gov/v1/agents/hr-copilot/runs", headers=CONNECTOR, json={"status": "weird"}).status_code == 400

    # 5. external turns show up in Monitor like internal ones (escalation in the inbox)
    inbox = ok(p.gov("GET", "/api/v1/ops/runs?review=open&agent=hr-copilot", ADMIN))["runs"]
    assert [r["run_id"] for r in inbox] == [esc["check_id"]] and inbox[0]["mode"] == "external"
    allr = ok(p.gov("GET", "/api/v1/ops/runs?agent=hr-copilot", ADMIN))["runs"]
    assert len(allr) == 5
    stored = ok(p.gov("GET", f"/api/v1/ops/runs/{red['check_id']}", ADMIN))
    assert "eve@example.com" not in stored["input"]["message"]  # personal data never stored
    done = next(r for r in allr if r["run_id"] == fine["check_id"])
    assert done["status"] == "ok" and done["on_behalf_of"] == "eve@example.com"
    stats = ok(p.gov("GET", "/api/v1/ops/stats", ADMIN))["agents"][0]
    assert stats["agent"] == "hr-copilot" and stats["escalated"] == 1 and stats["refused"] == 1


def test_http_agent_eval_target_and_edge_mode(world):
    p = build(gov_overrides={"relaxed_release": True}, external_transport=httpx.MockTransport(world.handler), edge=True)
    make_agent(p, "hr-http", {"platform": "http", "endpoint": "https://agent.example.com/chat", "auth_secret": "HTTP_AGENT_TOKEN",
                              "callers": ["copilot-connector"]})
    ok(p.gov("POST", "/api/v1/agents/hr-http/evals/suites", ADMIN, json={
        "name": "smoke", "required": True, "cases": [{"id": "x", "context": "staff", "input": {"message": "hello"},
                                                      "assertions": [{"type": "contains", "path": "output", "value": "[staff]"}]}]}), 201)
    run = ok(p.gov("POST", "/api/v1/agents/hr-http/evals/run?wait=true", ADMIN, json={}))["runs"][0]
    assert run["passed"] == 1, run
    ok(p.gov("POST", "/api/v1/agents/hr-http/quick-publish", ADMIN, json={}))
    assert world.hooks == []  # no webhook configured
    with p.rt_client() as rt:  # edge deployment: governance API and MCP only
        assert rt.get("/gov/v1/agents/hr-http/profile", headers=CONNECTOR).status_code == 200
        assert rt.post("/v1/agents/hr-http@prod/invoke", json={"input": {}}, headers=ADMIN).status_code == 404
        assert rt.post("/internal/v1/execute", json={}, headers=CONNECTOR).status_code == 404
        assert rt.get("/healthz").status_code == 200


def test_missing_secret_is_a_clear_eval_error(world, monkeypatch):
    monkeypatch.delenv("DL_SECRET")
    p = build(gov_overrides={"relaxed_release": True}, external_transport=httpx.MockTransport(world.handler))
    make_agent(p, "hr-nosecret", {"platform": "copilot_studio", "auth_secret": "DL_SECRET"})
    ok(p.gov("POST", "/api/v1/agents/hr-nosecret/evals/suites", ADMIN, json={
        "name": "smoke", "cases": [{"id": "x", "input": {"message": "hi"}}]}), 201)
    run = ok(p.gov("POST", "/api/v1/agents/hr-nosecret/evals/run?wait=true", ADMIN, json={}))["runs"][0]
    assert run["status"] == "error" and "DL_SECRET is not set" in run["error"]
