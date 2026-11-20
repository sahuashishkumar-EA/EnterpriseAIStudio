"""Intelligent web: smart site crawl, live web search, scheduled refresh of live web knowledge."""

import asyncio

import httpx
import pytest

from eap_governor import store
from eap_runtime.webscan import WebScanner, keywords, relevance, robots_disallows
from eap_runtime.websearch import WebSearcher, domain_ok, scoped_query
from tests.harness import build

ADMIN = {"X-Dev-User": "admin@example.com", "X-Dev-Roles": "*:platform_admin"}
SVC = {"X-Dev-User": "governor", "X-Dev-Kind": "service"}

PAGE = "<html><head><title>{t}</title></head><body><p>{body}</p>{links}</body></html>"
TEXT = "This page explains our products in detail for customers who want to know more. " * 3


def site(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\nSitemap: https://bank.example/sitemap.xml\n")
    if path == "/sitemap.xml":
        locs = "".join(f"<url><loc>https://bank.example{p}</loc></url>" for p in
                       ("/about", "/careers/jobs", "/products/savings-accounts", "/private/admin", "/press/2019"))
        return httpx.Response(200, text=f'<?xml version="1.0"?><urlset>{locs}</urlset>', headers={"content-type": "application/xml"})
    links = {"/": '<a href="/products/mortgage">Mortgage rates and conditions</a><a href="/blog/x">Blog</a>'
                  '<a href="/careers/jobs">Jobs</a><a href="https://other.example/">Partner</a>'}.get(path, "")
    if path.startswith("/private"):
        return httpx.Response(500)
    return httpx.Response(200, text=PAGE.format(t=f"Bank {path}", body=TEXT + path, links=links),
                          headers={"content-type": "text/html"})


def test_smart_site_crawl_follows_relevance_robots_and_patterns():
    s = WebScanner(transport=httpx.MockTransport(site), ssrf_protection=False)
    docs, report = asyncio.run(s.scan([{"url": "https://bank.example/", "mode": "site", "max_pages": 3,
                                        "exclude": ["/blog"]}], ["Mortgage advice", "savings accounts"]))
    got = [d["uri"] for d in docs]
    assert got[0] == "https://bank.example/"
    # the two most relevant pages next: mortgage (link text) and savings (sitemap URL); not careers/press
    assert set(got[1:]) == {"https://bank.example/products/mortgage", "https://bank.example/products/savings-accounts"}
    assert not any("/private" in u or "/blog" in u or "other.example" in u for u in got)
    assert "site mode: 5 sitemap URL(s), 1 robots rule(s)" in report[0]["info"]


def test_crawl_helpers():
    kw = keywords(["Mortgage advice for first-time buyers"])
    assert {"mortgage", "advice", "first", "time", "buyers"} <= kw
    assert relevance("https://x/products/mortgage", "", kw) > relevance("https://x/careers", "", kw)
    assert robots_disallows("User-agent: googlebot\nDisallow: /a\nUser-agent: *\nDisallow: /b # x\n") == ["/b"]


def test_web_search_helpers_and_gemini_parsing():
    assert domain_ok("https://www.ing.nl/x", ["ing.nl"], []) and domain_ok("https://sub.ing.nl", ["ing.nl"], [])
    assert not domain_ok("https://evil.example/ing.nl", ["ing.nl"], [])
    assert not domain_ok("https://forum.example.com/a", [], ["example.com"])
    assert scoped_query("rates", ["a.com", "b.com"], ["c.com"]) == "rates (site:a.com OR site:b.com) -site:c.com"
    data = {"candidates": [{"content": {"parts": [{"text": "The savings rate is 2.1%."}]},
                            "groundingMetadata": {
                                "groundingChunks": [{"web": {"uri": "https://www.bank.example/rates", "title": "bank.example"}},
                                                    {"web": {"uri": "https://news.example/a", "title": "news.example"}}],
                                "groundingSupports": [{"segment": {"text": "The savings rate is 2.1%."}, "groundingChunkIndices": [0]}]}}]}
    res = WebSearcher.parse(data)
    assert res[0].title == "Web search summary" and res[0].text == "The savings rate is 2.1%."
    assert (res[1].uri, res[1].text) == ("https://www.bank.example/rates", "The savings rate is 2.1%.")


@pytest.fixture
def plat(monkeypatch):
    monkeypatch.setenv("EAP_WEB_SEARCH", "mock")
    return build(gov_overrides={"relaxed_release": True}, web_transport=httpx.MockTransport(site), web_ssrf_protection=False)


def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def test_live_web_search_in_answers(plat):
    g = plat.gov
    ok(g("POST", "/api/v1/agents", ADMIN, json={"name": "rates-bot", "owner_bu": "retail"}), 201)
    ctx = {"name": "Customers", "role": "answers questions about interest rates",
           "web_search": {"enabled": True, "when": "always", "allowed_domains": ["bank.example"], "max_results": 2}}
    ok(g("PUT", "/api/v1/agents/rates-bot/draft/contexts/default", ADMIN, json={"context": ctx}))
    r = ok(g("POST", "/api/v1/agents/rates-bot/preview", ADMIN, json={"message": "What is the savings rate?"}))
    web = [s for s in r["sources"] if s["knowledge_ref"] == "web-search"]
    assert len(web) == 2 and all(s["uri"].startswith("https://bank.example/") for s in web)
    assert any(st["kind"] == "retrieval" and st["name"] == "web search" for st in r["trace"]["steps"])

    # confidential agents may not send questions to a search engine
    d = ok(g("GET", "/api/v1/agents/rates-bot", ADMIN))["draft"]["definition"]
    val = ok(g("POST", "/api/v1/agents/rates-bot/draft/validate", ADMIN,
               json={"definition": {**d, "data_classification": "confidential"}}))
    assert any(v["code"] == "web_search.classification" for v in val["violations"])


def test_scheduled_refresh_of_live_web_knowledge(plat):
    g = plat.gov
    ok(g("POST", "/api/v1/agents", ADMIN, json={"name": "site-bot", "owner_bu": "retail"}), 201)
    ctx = {"name": "Customers", "role": "explains mortgages", "web_refresh_days": 7,
           "web_sources": [{"url": "https://bank.example/", "mode": "site", "max_pages": 3}]}
    ok(g("PUT", "/api/v1/agents/site-bot/draft/contexts/default", ADMIN, json={"context": ctx}))
    scan = ok(g("POST", "/api/v1/agents/site-bot/contexts/default/scan?wait=true", ADMIN))
    assert scan["status"] == "completed" and scan["pages"] == 3
    ok(g("POST", "/api/v1/agents/site-bot/quick-publish", ADMIN, json={}))
    # just published: nothing due; 8 days later: due
    assert asyncio.run(plat.gov_db.call(store.refresh_plan)) == []
    later = asyncio.run(plat.gov_db.call(store.refresh_plan, "2099-01-01T00:00:00+00:00"))
    assert [(x["agent"], x["version"], x["context"]) for x in later] == [("site-bot", "1.0.0", "default")]
    # make it due and run the daily job
    asyncio.run(plat.gov_db.call(lambda tx: tx.execute(
        "UPDATE catalog_knowledge SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (later[0]["source_id"],))))
    started = ok(g("POST", "/internal/v1/tasks/rescan-due?wait=true", SVC))["started"]
    assert len(started) == 1
    assert asyncio.run(plat.gov_db.call(store.refresh_plan)) == []  # refreshed
    audit = ok(g("GET", "/api/v1/audit?target=agent/site-bot", ADMIN))["events"]
    assert any(e["action"] == "context.refresh" and e["details"]["version"] == "1.0.0" for e in audit)
