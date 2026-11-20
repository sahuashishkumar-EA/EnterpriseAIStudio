"""Batch 1 safety: versioned web knowledge, business-unit guard."""

import asyncio

import httpx

from tests.harness import build

ME = {"X-Dev-User": "me@example.com", "X-Dev-Roles": "*:platform_admin"}
PAGE = {"text": "Easy-access savings pays 1.75 percent variable interest on all balances at Example Bank."}


def web(req: httpx.Request) -> httpx.Response:
    body = f"<html><head><title>Savings</title></head><body><p>{PAGE['text']}</p></body></html>"
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def call(p, method, path, headers=ME, status=200, **kw):
    r = p.gov(method, f"/api/v1{path}", headers, **kw)
    assert r.status_code == status, r.text
    return r.json()


def test_rescanning_a_draft_does_not_change_the_live_version():
    p = build(gov_overrides={"relaxed_release": True}, web_transport=httpx.MockTransport(web), web_ssrf_protection=False)
    call(p, "POST", "/agents", json={"name": "saver", "owner_bu": "retail"}, status=201)
    ctx = {"name": "Retail", "web_sources": [{"url": "https://bank.example/savings", "max_pages": 1}],
           "starters": ["What is the savings rate?"]}
    call(p, "PUT", "/agents/saver/draft/contexts/default", json={"context": ctx})
    call(p, "POST", "/agents/saver/contexts/default/scan")
    q = call(p, "POST", "/agents/saver/quick-publish", json={})
    assert q["version"] == "1.0.0" and not q.get("warnings")
    models = {m["id"] for m in call(p, "GET", "/catalog/knowledge")["knowledge"]}
    assert {"web-saver-default", "web-saver-default--v1-0-0"} <= models

    PAGE["text"] = "Changed page: savings now pays 9.99 percent bonus interest for everybody immediately."
    call(p, "POST", "/agents/saver/contexts/default/scan")  # re-scan the draft only
    with p.rt_client() as c:
        live = c.post("/v1/agents/saver@prod/invoke", json={"input": {"message": "savings interest rate"}},
                      headers={"X-Dev-User": "ann@example.com", "X-Dev-Roles": "retail:viewer"}).json()
    assert live["status"] == "ok"
    assert all(s["knowledge_ref"] == "web-saver-default--v1-0-0" for s in live["sources"])
    assert "1.75" in live["sources"][0]["snippet"] and "9.99" not in str(live["sources"])
    draft = call(p, "POST", "/agents/saver/preview", json={"message": "savings interest rate"})
    assert any("9.99" in s["snippet"] for s in draft["sources"])  # the draft sees the new scan
    PAGE["text"] = "Easy-access savings pays 1.75 percent variable interest on all balances at Example Bank."


def test_business_units_cannot_be_created_by_typo():
    p = build()
    author = {"X-Dev-User": "al@example.com", "X-Dev-Roles": "retail:author"}
    call(p, "POST", "/agents", headers=author, json={"name": "a1", "owner_bu": "retail"}, status=201)
    r = p.gov("POST", "/api/v1/agents", author, json={"name": "a2", "owner_bu": "Retail"})
    assert r.status_code in (403, 422)  # no role and unknown BU
    multi = {"X-Dev-User": "bo@example.com", "X-Dev-Roles": "*:author"}
    r = p.gov("POST", "/api/v1/agents", multi, json={"name": "a3", "owner_bu": "retial"})
    assert r.status_code == 422 and "does not exist" in r.text
    call(p, "POST", "/agents", json={"name": "a4", "owner_bu": "newbu"}, status=201)  # platform admins may
    bus = call(p, "GET", "/bus", headers=multi)
    assert {"retail", "newbu"} <= set(bus["bus"]) and bus["can_create"] is False
    assert call(p, "GET", "/settings")["dev_auth"] is True
