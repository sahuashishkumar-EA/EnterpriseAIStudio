"""Batch 3: background scans, save-as-test, model usage, quality hints."""

import asyncio
import time

import httpx

from tests.harness import build

ME = {"X-Dev-User": "me@example.com", "X-Dev-Roles": "*:platform_admin"}


def web(req):
    return httpx.Response(200, text="<html><title>T</title><body><p>" + "Opening hours are nine to five on weekdays. " * 4 +
                          "</p></body></html>", headers={"content-type": "text/html"})


def call(p, method, path, status=200, **kw):
    r = p.gov(method, f"/api/v1{path}", ME, **kw)
    assert r.status_code == status, r.text
    return r.json()


def test_background_scan_save_as_test_and_usage():
    p = build(gov_overrides={"relaxed_release": True}, web_transport=httpx.MockTransport(web), web_ssrf_protection=False)

    async def go():
        async with p.gov_client() as g:
            assert (await g.post("/api/v1/agents", json={"name": "hours", "owner_bu": "ops"}, headers=ME)).status_code == 201
            ctx = {"name": "Staff", "role": "x", "web_sources": [{"url": "https://ex.example/hours", "max_pages": 1}],
                   "starters": ["When are you open?"], "guardrails": {"blocked_topics": ["salary"]}}
            await g.put("/api/v1/agents/hours/draft/contexts/default", json={"context": ctx}, headers=ME)
            r = await g.post("/api/v1/agents/hours/contexts/default/scan", headers=ME)
            assert r.status_code == 202 and r.json()["status"] == "running"
            await p.governor.state.gov.tasks.drain()  # local queue: run the background job
            scans = (await g.get("/api/v1/agents/hours/contexts/default/scans", headers=ME)).json()["scans"]
            assert scans[0]["status"] == "completed" and scans[0]["pages"] == 1
            comp = (await g.get("/api/v1/agents/hours/compose?context=default", headers=ME)).json()["completeness"]
            assert any("refusal reply is the generic default" in h for h in comp["hints"])
            assert any("role is very short" in h for h in comp["hints"])
    asyncio.run(go())

    c1 = call(p, "POST", "/agents/hours/evals/cases", 201, json={"case": {"context": "default", "input": {"message": "When are you open?"},
              "assertions": [{"type": "contains", "path": "output", "value": "When are you open"}]}})
    c2 = call(p, "POST", "/agents/hours/evals/cases", 201, json={"case": {"context": "default", "input": {"message": "what is the CEO salary"},
              "assertions": [{"type": "contains", "path": "flags", "value": "responded:topic_policy"}]}})
    assert c1["suite"] == "from-preview" and c2["cases"] == 2 and c1["case_id"] != c2["case_id"]
    run = call(p, "POST", "/agents/hours/evals/run?wait=true", json={"target": "draft"})["runs"][0]
    assert run["passed"] == run["total"] == 2
    q = call(p, "POST", "/agents/hours/quick-publish", json={})
    assert q["version"] == "1.0.0"
    usage = call(p, "GET", "/catalog/models/mock-small/usage")
    assert {"agent_name": "hours", "alias": "prod", "version": "1.0.0"} in usage["live"]
