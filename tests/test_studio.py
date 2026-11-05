"""Two-persona authoring: technical + functional experts on the same agent, multiple
functional contexts, web scanning, live preview, friendly functional guardrails, quick publish."""

import asyncio

import httpx
import pytest

from tests.harness import build

FUNC = {"X-Dev-User": "fiona@example.com", "X-Dev-Roles": "lending:functional_author"}
TECH = {"X-Dev-User": "tom@example.com", "X-Dev-Roles": "lending:author"}
ME = {"X-Dev-User": "me@example.com", "X-Dev-Roles": "*:platform_admin"}

PAGES = {
    "https://bank.example/mortgages": ("<html><head><title>Mortgages</title></head><body><nav>menu</nav>"
                                       "<h1>Mortgages</h1><p>Our fixed-rate mortgage runs for 10 or 20 years. The maximum "
                                       "loan is 4.5 times your gross annual income. Early repayment of up to 10% per year "
                                       "is free of charge.</p><a href='/mortgages/rates'>Rates</a>"
                                       "<a href='https://other.example/x'>elsewhere</a></body></html>"),
    "https://bank.example/mortgages/rates": ("<html><head><title>Rates</title></head><body><p>Current 10-year fixed "
                                             "rate is 3.4 percent. The 20-year fixed rate is 3.7 percent. Rates change "
                                             "weekly on Mondays.</p><script>var x=1</script></body></html>"),
}


def web(req: httpx.Request) -> httpx.Response:
    body = PAGES.get(str(req.url))
    if body is None:
        return httpx.Response(404, text="nope")
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def plat():
    return build(gov_overrides={"relaxed_release": True}, web_transport=httpx.MockTransport(web),
                 web_ssrf_protection=False)


def call(plat, method, path, headers, status=200, **kw):
    r = plat.gov(method, f"/api/v1{path}", headers, **kw)
    assert r.status_code == status, r.text
    return r.json()


RETAIL = {
    "name": "Retail customers", "audience": "existing retail customers on the website",
    "role": "a friendly mortgage assistant of Example Bank", "goal": "Explain our mortgage products clearly",
    "behavior": {"tone": "friendly", "verbosity": "brief", "language": "match the user", "emoji": False},
    "always": ["Mention that rates change weekly"], "never": ["Give personal financial advice"],
    "notes": [{"title": "Opening hours", "text": "Advisors are available Mon-Fri 9:00-17:00."}],
    "web_sources": [{"url": "https://bank.example/mortgages", "max_pages": 3}],
    "guardrails": {"blocked_topics": ["crypto", "gambling"], "refusal_message": "I can only help with mortgages.",
                   "escalation": {"triggers": ["complaint", "speak to a human"], "message": "Connecting you to an advisor."},
                   "personal_data": "redact", "disclaimer": "This is general information, not advice."},
    "examples": [{"user": "Can I repay early?", "assistant": "Yes, up to 10% per year without a fee."}],
    "starters": ["What mortgage terms do you offer?", "How much can I borrow?"],
}
BROKER = {"name": "Mortgage brokers", "role": "a precise product specialist for intermediaries",
          "behavior": {"tone": "formal", "verbosity": "detailed"}, "starters": ["What is the max LTI?"]}


def test_two_personas_and_functional_contexts(plat):
    call(plat, "POST", "/agents", FUNC, 201, json={"name": "mortgage-helper", "owner_bu": "lending",
                                                   "description": "Answers mortgage questions"})
    d = call(plat, "GET", "/agents/mortgage-helper", FUNC)["draft"]
    assert d["definition"]["exposure"]["chat"] and d["definition"]["default_context"] == "default"

    # functional expert cannot touch the technical setup
    r = plat.gov("PUT", "/api/v1/agents/mortgage-helper/draft/technical", FUNC, json={"definition": d["definition"]})
    assert r.status_code == 403

    # both edit concurrently: functional saves contexts, technical saves the model, nobody clobbers anyone
    tech_base = d["sections"]["technical"]
    call(plat, "PUT", "/agents/mortgage-helper/draft/contexts/retail", FUNC, json={"context": RETAIL, "base": ""})
    call(plat, "PUT", "/agents/mortgage-helper/draft/contexts/broker", FUNC, json={"context": BROKER, "base": ""})
    call(plat, "PUT", "/agents/mortgage-helper/draft/default-context", FUNC, json={"context": "retail"})
    call(plat, "DELETE", "/agents/mortgage-helper/draft/contexts/default", FUNC)
    tech = {**d["definition"], "model": {"catalog_id": "mock-large"}, "contexts": [], "default_context": None}
    call(plat, "PUT", "/agents/mortgage-helper/draft/technical", TECH, json={"definition": tech, "base": tech_base})
    d2 = call(plat, "GET", "/agents/mortgage-helper", TECH)["draft"]
    assert d2["definition"]["model"]["catalog_id"] == "mock-large"
    assert [c["id"] for c in d2["definition"]["contexts"]] == ["retail", "broker"]  # untouched by the technical save
    assert d2["definition"]["default_context"] == "retail"
    assert set(d2["editors"]) >= {"fiona@example.com", "tom@example.com"}

    # stale base -> conflict instead of silent overwrite
    r = plat.gov("PUT", "/api/v1/agents/mortgage-helper/draft/technical", TECH, json={"definition": tech, "base": tech_base})
    assert r.status_code == 409
    r = plat.gov("PUT", "/api/v1/agents/mortgage-helper/draft/contexts/retail", FUNC,
                 json={"context": RETAIL, "base": "stale"})
    assert r.status_code == 409

    # what the model sees: technical system context + functional context + platform rules
    comp = call(plat, "GET", "/agents/mortgage-helper/compose?context=retail", FUNC)
    sp = comp["system_prompt"]
    assert "# System context" in sp and "friendly mortgage assistant" in sp and "Never" in sp
    assert "Opening hours" in sp and "Platform rules" in sp
    assert {g["type"] for g in comp["guardrails"]["functional"]} >= {"topic_policy", "escalation", "pii_redaction"}
    assert comp["knowledge"] == ["web-mortgage-helper-retail"] and comp["completeness"]["score"] == 100
    broker = call(plat, "GET", "/agents/mortgage-helper/compose?context=broker", FUNC)
    assert "product specialist" in broker["system_prompt"] and "friendly mortgage" not in broker["system_prompt"]

    # scan the context's web pages (same domain only, script/nav stripped)
    scan = call(plat, "POST", "/agents/mortgage-helper/contexts/retail/scan", FUNC)
    assert scan["pages"] == 2 and scan["chunks"] >= 2
    assert all("other.example" not in p["url"] for p in scan["report"])
    scans = call(plat, "GET", "/agents/mortgage-helper/contexts/retail/scans", FUNC)["scans"]
    assert scans[0]["status"] == "completed"

    # live preview of the draft: grounded in scanned pages, disclaimer appended, history accepted
    pv = call(plat, "POST", "/agents/mortgage-helper/preview", FUNC,
              json={"context": "retail", "message": "What is the 10-year fixed rate?",
                    "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"}]})
    assert pv["status"] == "ok" and pv["context"] == "retail"
    assert pv["sources"] and any("rates" in s["uri"] for s in pv["sources"])
    assert "not advice" in pv["output"]
    # functional guardrails answer politely and flag the run
    ref = call(plat, "POST", "/agents/mortgage-helper/preview", FUNC, json={"context": "retail", "message": "Should I buy crypto?"})
    assert ref["status"] == "ok" and ref["output"] == "I can only help with mortgages." and "responded:topic_policy" in ref["flags"]
    esc = call(plat, "POST", "/agents/mortgage-helper/preview", FUNC, json={"context": "retail", "message": "I have a complaint!"})
    assert esc["output"] == "Connecting you to an advisor." and "escalated" in esc["flags"]
    pii = call(plat, "POST", "/agents/mortgage-helper/preview", FUNC,
               json={"context": "retail", "message": "my email is jan@example.org, rates?"})
    assert pii["status"] == "ok" and "jan@example.org" not in str(pii)
    # the broker context has no such boundaries
    b = call(plat, "POST", "/agents/mortgage-helper/preview", FUNC, json={"context": "broker", "message": "crypto?"})
    assert b["flags"] == [] and b["context"] == "broker"


def test_quick_publish_and_chat_channel(plat):
    call(plat, "POST", "/agents", ME, 201, json={"name": "helpdesk", "owner_bu": "it"})
    call(plat, "PUT", "/agents/helpdesk/draft/contexts/default", ME,
         json={"context": {"name": "Employees", "role": "the IT helpdesk", "starters": ["How do I reset my password?"]}})
    q = call(plat, "POST", "/agents/helpdesk/quick-publish", ME, json={})
    assert q["version"] == "1.0.0" and q["alias"]["alias"] == "prod" and q["smoke_suite_created"]
    # same person authored, approved and published - allowed only because the governor runs in relaxed (dev) mode
    audit = call(plat, "GET", "/audit?target=agent/helpdesk", ME)["events"]
    assert {"draft.approve", "version.publish", "alias.move"} <= {e["action"] for e in audit}
    # second quick publish without changes just re-points the alias
    assert call(plat, "POST", "/agents/helpdesk/quick-publish", ME, json={})["published"] is False
    agents = plat.gov("GET", "/internal/v1/agents?alias=prod&channel=chat",
                      {"X-Dev-User": "chat", "X-Dev-Kind": "service"}).json()["agents"]
    assert agents[0]["name"] == "helpdesk" and agents[0]["contexts"][0]["starters"]


def test_quick_publish_disabled_outside_dev():
    p = build()
    call(p, "POST", "/agents", ME, 201, json={"name": "helpdesk", "owner_bu": "it"})
    assert p.gov("POST", "/api/v1/agents/helpdesk/quick-publish", ME, json={}).status_code == 403
    # separation of duties still enforced: the author cannot approve
    r = p.gov("POST", "/api/v1/agents/helpdesk/approvals", ME, json={"decision": "approve"})
    assert r.status_code == 409


def test_model_test_endpoint(plat):
    ok = call(plat, "POST", "/catalog/models/mock-large/test", ME)
    assert ok["ok"] and ok["reply"]
    assert plat.gov("POST", "/api/v1/catalog/models/mock-large/test", FUNC).status_code == 403


def test_scanner_blocks_private_hosts():
    from eap_runtime.webscan import ScanError, check_public

    for url in ("http://127.0.0.1/", "http://169.254.169.254/computeMetadata/v1/", "http://localhost/",
                "http://metadata.google.internal/", "ftp://example.com/", "http://10.0.0.5/"):
        with pytest.raises(ScanError):
            asyncio.run(check_public(url))
