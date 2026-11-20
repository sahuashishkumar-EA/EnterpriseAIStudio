"""BU variants: the technical owner keeps the core; a BU shapes its own variant within what the core allows."""

from tests.harness import build

TECH = {"X-Dev-User": "tom@example.com", "X-Dev-Roles": "ai-platform:bu_admin"}
RETAIL = {"X-Dev-User": "rita@example.com", "X-Dev-Roles": "retail:functional_author,retail:approver,retail:publisher"}
WEALTH = {"X-Dev-User": "will@example.com", "X-Dev-Roles": "wealth:functional_author"}
CUSTOMER = {"X-Dev-User": "ann@example.com", "X-Dev-Roles": "retail:member"}

CORE_PROMPT = "You are the bank's assistant. Never give investment advice."


def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def make_core(p):
    g = p.gov
    ok(g("POST", "/api/v1/agents", TECH, json={"name": "bank-assistant", "owner_bu": "ai-platform",
                                                "description": "Answers banking questions"}), 201)
    d = ok(g("GET", "/api/v1/agents/bank-assistant", TECH))["draft"]
    tech = {**d["definition"], "prompt": {"system": CORE_PROMPT}, "limits": {"max_cost_usd": 2},
            "customization": {"enabled": True, "allowed_bus": ["retail", "wealth"], "models": ["mock-large"],
                              "contract": False}}
    ok(g("PUT", "/api/v1/agents/bank-assistant/draft/technical", TECH, json={"definition": tech, "base": d["sections"]["technical"]}))
    assert ok(g("POST", "/api/v1/agents/bank-assistant/quick-publish", TECH, json={}))["version"] == "1.0.0"


def test_bu_variant_lifecycle():
    p = build(gov_overrides={"relaxed_release": True})
    g = p.gov
    make_core(p)

    # the BU's functional expert creates their variant (not the core team)
    assert g("POST", "/api/v1/agents/bank-assistant/variants", WEALTH, json={"bu": "retail"}).status_code == 403
    v = ok(g("POST", "/api/v1/agents/bank-assistant/variants", RETAIL, json={"bu": "retail"}), 201)
    assert v["name"] == "bank-assistant-retail" and v["variant_of"] == "bank-assistant" and v["owner_bu"] == "retail"
    assert v["draft"]["definition"]["extends"] == {"agent": "bank-assistant", "version": "1.0.0"}
    assert v["core"]["live_version"] == "1.0.0" and v["core"]["customization"]["models"] == ["mock-large"]
    assert g("POST", "/api/v1/agents/bank-assistant/variants", RETAIL, json={"bu": "retail"}).status_code == 409

    # BU shapes its own behaviour: context, BU rules, model, tighter limits
    ctx = {"name": "Retail customers", "role": "the retail banking assistant", "starters": ["What is the savings rate?"]}
    ok(g("PUT", "/api/v1/agents/bank-assistant-retail/draft/contexts/default", RETAIL, json={"context": ctx}))
    base = ok(g("GET", "/api/v1/agents/bank-assistant-retail", RETAIL))["draft"]["sections"]["bu"]
    ok(g("PUT", "/api/v1/agents/bank-assistant-retail/draft/bu", RETAIL, json={
        "base": base, "overlay": {"instructions": "Always mention the Retail service number 0800-123.",
                                  "model": {"catalog_id": "mock-large"}, "limits": {"max_cost_usd": 1}},
        "exposure": {"chat": True, "callable_by_agents": False}}))
    # but not the core's technical setup
    d = ok(g("GET", "/api/v1/agents/bank-assistant-retail", RETAIL))["draft"]
    r = g("PUT", "/api/v1/agents/bank-assistant-retail/draft/technical",
          {**RETAIL, "X-Dev-Roles": "retail:author"}, json={"definition": d["definition"], "base": d["sections"]["technical"]})
    assert r.status_code == 422 and "technical setup comes from bank-assistant" in r.text
    assert g("PUT", "/api/v1/agents/bank-assistant-retail/draft", {**RETAIL, "X-Dev-Roles": "retail:author"},
             json={**d["definition"], "extends": {"agent": "other", "version": "1.0.0"}}).status_code == 422

    # what the model sees: core prompt + BU rules + BU context
    sp = ok(g("GET", "/api/v1/agents/bank-assistant-retail/compose", RETAIL))["system_prompt"]
    assert CORE_PROMPT in sp and "Rules of business unit retail" in sp and "0800-123" in sp and "retail banking assistant" in sp

    # outside what the core allows -> validation violations
    bad = {**d["definition"], "overlay": {"model": {"catalog_id": "mock-small-x"}, "limits": {"max_cost_usd": 5},
                                          "contract": {"description": "mine"}}}
    val = ok(g("POST", "/api/v1/agents/bank-assistant-retail/draft/validate", RETAIL, json={"definition": bad}))
    assert {x["code"] for x in val["violations"]} >= {"variant.model", "variant.limits", "variant.contract"}

    # the BU releases its variant on its own (its approvers, its tests)
    pub = ok(g("POST", "/api/v1/agents/bank-assistant-retail/quick-publish", RETAIL, json={}))
    assert pub["version"] == "1.0.0"
    with p.rt_client() as rt:
        out = ok(rt.post("/v1/agents/bank-assistant-retail@prod/invoke", headers=CUSTOMER,
                         json={"input": {"message": "What is the savings rate?"}}))
        assert out["status"] == "ok" and "mock-large" in out["output"]  # the BU's model choice
        bundle = ok(rt.get("/healthz"))
        assert bundle["ok"]

    # core moves on; the live variant keeps running on the core version it was tested with
    d = ok(g("GET", "/api/v1/agents/bank-assistant", TECH))["draft"]
    tech = {**d["definition"], "prompt": {"system": CORE_PROMPT + " Be concise."}}
    ok(g("PUT", "/api/v1/agents/bank-assistant/draft/technical", TECH, json={"definition": tech, "base": d["sections"]["technical"]}))
    ok(g("POST", "/api/v1/agents/bank-assistant/quick-publish", TECH, json={}))
    core = ok(g("GET", "/api/v1/agents/bank-assistant", TECH))
    assert core["variants"] == [{"name": "bank-assistant-retail", "bu": "retail", "draft_core_version": "1.0.0",
                                 "live_version": "1.0.0", "live_core_version": "1.0.0", "model": "mock-large"}]
    var = ok(g("GET", "/api/v1/agents/bank-assistant-retail", RETAIL))
    assert var["core"]["live_version"] == "1.0.1"
    live_sp = ok(g("GET", "/internal/v1/resolve/bank-assistant-retail/prod", {"X-Dev-User": "runtime", "X-Dev-Kind": "service"}))
    assert "Be concise" not in live_sp["primary"]["definition"]["prompt"]["system"]
    assert live_sp["primary"]["variant_of"] == {"agent": "bank-assistant", "version": "1.0.0"}

    # the BU adopts the new core when ready: rebase keeps their parts, then test and release again
    rb = ok(g("POST", "/api/v1/agents/bank-assistant-retail/rebase", RETAIL, json={}))
    assert rb["definition"]["extends"]["version"] == "1.0.1"
    assert rb["definition"]["overlay"]["instructions"].startswith("Always mention")
    assert rb["definition"]["contexts"][0]["role"] == "the retail banking assistant"
    ok(g("POST", "/api/v1/agents/bank-assistant-retail/quick-publish", RETAIL, json={}))
    live = ok(g("GET", "/internal/v1/resolve/bank-assistant-retail/prod", {"X-Dev-User": "runtime", "X-Dev-Kind": "service"}))
    assert "Be concise" in live["primary"]["definition"]["prompt"]["system"]

    # core owners can look at (not edit) their variants; other BUs cannot
    assert g("GET", "/api/v1/agents/bank-assistant-retail", TECH).status_code == 200
    assert g("GET", "/api/v1/agents/bank-assistant-retail", WEALTH).status_code == 403
    assert g("PUT", "/api/v1/agents/bank-assistant-retail/draft/bu", TECH, json={"overlay": {}}).status_code == 403


def test_core_controls_who_may_customize():
    p = build(gov_overrides={"relaxed_release": True})
    g = p.gov
    ok(g("POST", "/api/v1/agents", TECH, json={"name": "closed-core", "owner_bu": "ai-platform"}), 201)
    ok(g("POST", "/api/v1/agents/closed-core/quick-publish", TECH, json={}))
    r = g("POST", "/api/v1/agents/closed-core/variants", RETAIL, json={"bu": "retail"})
    assert r.status_code == 422 and "does not allow business-unit variants" in r.text
    make_core(p)
    ok(g("POST", "/api/v1/agents/bank-assistant/variants", WEALTH, json={"bu": "wealth"}), 201)
    # a variant of a variant is not possible
    r = g("POST", "/api/v1/agents/bank-assistant-wealth/variants", RETAIL, json={"bu": "retail"})
    assert r.status_code == 422
