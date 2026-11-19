"""Unit tests for the configuration-first platform (C01, C03, C05, C06, C07, C10), by requirement ID."""

from __future__ import annotations

import json

import pytest

import eap_metamodel
from eap_compiler import UnsupportedFields, check_supported, compile_spec
from eap_compiler.adapters import MockAdapter
from eap_compiler.secrets import EnvFileSecrets
from eap_core.events import Event
from eap_metamodel import diff_paths, migrate
from eap_store import store, seed

from tests.e2e.platform import AGENT, build_platform


@pytest.fixture(scope="module")
def mm():
    return eap_metamodel.load()


@pytest.fixture()
def p():
    plat = build_platform()
    yield plat
    plat.loop.close()


# ------------------------------------------------------------------ C01 metamodel


def test_c01_f1_pilot_expressed_without_vendor_fields(mm):
    for a in seed.load_seed():
        assert mm.validate(a["type"], a["content"]) == [], a["name"]
    blob = json.dumps(seed.load_seed()).lower()
    for vendor in ("vertex", "gemini", "foundry", "copilot", "bedrock", "dialogflow"):
        assert vendor not in blob


def test_c01_f2_f3_every_field_has_lane_and_editors(mm):
    for t, s in mm.schemas.items():
        for field in s["properties"]:
            ann = mm.annotations(t, f"/{field}")
            assert ann.get("x-lane") in ("hot", "cold"), (t, field)
            assert ann.get("x-editors"), (t, field)


def test_c01_f2_classification(mm):
    old = {"bindingType": "tool", "agentId": "a", "tools": [{"capability": "c", "toolDefId": "t"}]}
    new = {**old, "tools": old["tools"] + [{"capability": "d", "toolDefId": "u"}]}
    c = mm.classify("binding", old, new)
    assert c.cold == ["/tools/1"] and c.hot == [] and c.approvers == {"security"}
    p_old = seed.load_seed()[14]["content"]
    p_new = {**p_old, "persona": {**p_old["persona"], "tone": "formal"}}
    c = mm.classify("profile", p_old, p_new)
    assert c.hot == ["/persona/tone"] and c.approvers == {"bu_owner"}


def test_c01_f4_limits(mm):
    limits = [{"appliesTo": "binding:thresholds", "path": "/values/maxExposureEur", "maximum": 10}]
    ok = {"bindingType": "thresholds", "agentId": "a", "values": {"maxExposureEur": 5}}
    assert mm.limit_violations("binding", ok, limits) == []
    bad = {**ok, "values": {"maxExposureEur": 11}}
    assert mm.limit_violations("binding", bad, limits)[0]["code"] == "limit"


def test_c01_f5_migration_is_lossless(mm):
    old = {"agentId": AGENT, "scope": {"businessUnit": "sales"}, "persona": "Sales helper", "tone": "friendly",
           "instructions": "Be brief.", "outputFormat": {"format": "text"}}
    new = migrate("profile", old, "1.0.0", mm.schema_version("profile"))
    assert mm.validate("profile", new) == []
    assert new["persona"] == {"name": "Sales helper", "tone": "friendly"}
    assert new["instructions"] == old["instructions"] and new["scope"] == old["scope"]


def test_diff_paths():
    assert diff_paths({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 3], "c": 0}) == ["/b/1", "/c"]


# ------------------------------------------------------------------ C03 registry


def test_c03_f3_optimistic_locking(p):
    lv = p.latest("profile", f"{AGENT}-service")
    c = {**lv["content"], "instructions": "one"}
    assert p.call("POST", f"/artifacts/{lv['artifact_id']}/drafts", {"content": c}, user="pat@example.com")[0] == 422
    s1, _ = p.call("POST", f"/artifacts/{lv['artifact_id']}/drafts", {"baseVersionId": lv["id"], "content": c},
                   roles=["*:platform_admin"])
    assert s1 == 201
    s2, body = p.call("POST", f"/artifacts/{lv['artifact_id']}/drafts", {"baseVersionId": lv["id"], "content": c},
                      roles=["*:platform_admin"])
    assert s2 == 409 and body["details"]["baseVersionId"] == lv["id"]


def test_c03_field_roles_enforced(p):
    # sally (sales business_unit) may edit persona, not the profile's agentId (architect only)
    s, body = p.draft("profile", f"{AGENT}-sales", lambda c: c.update(agentId="other-agent"), user="sally@example.com")
    assert s == 403 and body["details"][0]["path"] == "/agentId"
    # and not another BU's profile
    s, _ = p.draft("profile", f"{AGENT}-risk", lambda c: c["persona"].update(tone="friendly"), user="sally@example.com")
    assert s == 403


def test_c03_f4_hash_verified_on_read(p):
    vid = p.latest("role", f"{AGENT}-role")["id"]

    def tamper(tx):  # simulate tampering below the API (trigger removed by a DBA)
        tx.execute("DROP TRIGGER artifact_version_no_update")
        tx.execute("UPDATE artifact_version SET content = ? WHERE id = ?", (json.dumps({"purpose": "evil"}), vid))

    p.run(p.db.call(tamper))
    status, body = p.call("GET", f"/versions/{vid}")
    assert status == 500 and body["error"] == "integrity"


def test_c03_f5_dependents(p):
    skill = p.find("skill", "kyc-summary")
    _, out = p.call("GET", f"/artifacts/{skill['id']}/dependents")
    assert [d["name"] for d in out["items"]] == [f"{AGENT}-risk-skills"]
    policy = p.find("guardrail_policy", f"{AGENT}-guardrails")
    assert [d["name"] for d in p.call("GET", f"/artifacts/{policy['id']}/dependents")[1]["items"]] == [AGENT]


def test_c03_every_call_audited_and_openapi(p):
    n0 = p.run(p.db.call(lambda tx: tx.scalar("SELECT COUNT(*) FROM audit_log")))
    p.call("GET", "/artifacts?type=profile")
    n1 = p.run(p.db.call(lambda tx: tx.scalar("SELECT COUNT(*) FROM audit_log")))
    assert n1 == n0 + 1
    from tests.e2e.platform import asgi

    status, doc = p.run(asgi(p.registry, "GET", "/openapi.json"))
    assert doc["openapi"] == "3.1.0" and "/change-requests/{id}/approvals" in doc["paths"]


def test_c04_full_history_from_database_alone(p):
    _, draft = p.draft("profile", f"{AGENT}-sales", lambda c: c["persona"].update(tone="formal"), user="sally@example.com")
    _, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "formal", "environment": "dev"},
                   user="sally@example.com")
    p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
           user="bob@example.com")
    rows = p.run(p.db.call(lambda tx: tx.fetchall(
        "SELECT v.author, a.approver, p.environment FROM artifact_version v "
        "JOIN active_pointer p ON p.version_id = v.id JOIN approval a ON a.change_request_id = p.change_request_id "
        "WHERE v.id = ?", (draft["id"],))))
    assert rows == [{"author": "sally@example.com", "approver": "bob@example.com", "environment": "dev"}]


# ------------------------------------------------------------------ C05 workflow


def test_c05_f7_emergency_rollback_needs_after_the_fact_approval(p):
    _, draft = p.draft("profile", f"{AGENT}-sales", lambda c: c["persona"].update(tone="formal"), user="sally@example.com")
    _, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "x", "environment": "dev"},
                   user="sally@example.com")
    p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
           user="bob@example.com")
    art = p.find("profile", f"{AGENT}-sales")
    status, rb = p.call("POST", f"/artifacts/{art['id']}/rollback", {"environment": "dev", "emergency": True,
                                                                       "reason": "bad tone"}, user="pat@example.com")
    assert status == 200 and rb["state"] == "activated_pending_review"
    assert p.resolve("sales", env="dev")[1]["profile"]["persona"]["tone"] == "friendly"
    _, rb = p.call("POST", f"/change-requests/{rb['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
                   user="bob@example.com")
    assert rb["state"] == "activated"


def test_c05_f4_auto_approval_disabled_by_default(p):
    assert p.engine.settings.auto_approval_enabled is False


def test_c05_f5_promotion_cannot_skip_test(p):
    _, draft = p.draft("profile", f"{AGENT}-sales", lambda c: c["persona"].update(tone="formal"), user="sally@example.com")
    _, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "x", "environment": "dev"},
                   user="sally@example.com")
    _, cr = p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
                   user="bob@example.com")
    status, body = p.call("POST", f"/change-requests/{cr['id']}/promote", {"environment": "production"},
                          user="sally@example.com")
    assert status == 409 and "not active in test" in body["message"]


# ------------------------------------------------------------------ C06 / C07 / C10


def test_c06_f5_reevaluate_on_skill_change(p):
    from eap_evalrunner import EvalRunner

    skill = p.find("skill", "opportunity-scan")
    out = p.run(EvalRunner(p.db, p.mm).reevaluate_dependents(skill["id"], "production"))
    assert out and all(t["passed"] for t in out)


def test_c07_f3_activation_event_invalidates_cache(p):
    assert p.resolve("sales")[1]["source"] == "registry"
    assert p.resolve("sales")[1]["source"] == "cache"
    p.run(p.resolver.on_event(Event(event_type="config.version.activated", data={"environment": "production"})))
    assert p.resolve("sales")[1]["source"] == "registry"


def test_c10_f7_unsupported_fields_fail_early(p):
    snap = p.run(p.db.call(lambda tx: store.snapshot(tx, "production")))
    spec = compile_spec(snap, AGENT)
    with pytest.raises(UnsupportedFields, match="reasoning"):
        check_supported(MockAdapter(EnvFileSecrets(), unsupported=("reasoning",)), spec)


def test_c10_f6_credentials_from_vault(p, monkeypatch):
    monkeypatch.delenv("EAP_SECRET_MOCK_PLATFORM_TOKEN", raising=False)
    snap = p.run(p.db.call(lambda tx: store.snapshot(tx, "production")))
    a = MockAdapter(EnvFileSecrets())
    plan = a.plan(compile_spec(snap, AGENT), None, "dev")
    with pytest.raises(Exception, match="mock-platform-token"):
        p.run(a.apply(plan))


def test_c08_config_mcp_hot_fields_only(p):
    from eap_config_mcp import build
    from eap_core.auth import Principal

    svc = Principal(sub="config-mcp@svc", email="config-mcp@svc", kind="service")
    p.resolver.identities.trusted.add("config-mcp@svc")

    async def resolve(agent, bu, env):
        return await p.resolver.resolve(svc, agent, bu, env, on_behalf_of=agent)

    server = build(resolve, calling_agent=lambda: AGENT)
    res = p.run(server.call_tool("get_active_profile", {"agent_id": AGENT, "business_unit": "service"}))
    data = json.loads(res.content[0].text)
    assert set(data) == {"persona", "instructions", "output_format", "thresholds", "escalation_contacts", "versions"}
    assert data["persona"]["name"] == "Service profile assistant"
    assert server.served[-1]["versions"]["profile"] == data["versions"]["profile"]
