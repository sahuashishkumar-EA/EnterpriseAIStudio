"""Phase 1 end-to-end acceptance tests (spec section 10.1), one test per numbered item.

Run: python -m pytest -q tests/e2e
Test 6 also runs against a real PostgreSQL 16 when `psql` and a local server are available
(EAP_TEST_PG_SUPERUSER, default `postgres`), otherwise only the SQLite twin is checked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eap_agent_config import AgentConfig
from eap_compiler import compile_spec, drift
from eap_core.auth import Principal
from eap_store import MIGRATIONS_DIR, store

from .platform import AGENT, asgi, build_platform

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def p():
    plat = build_platform()
    yield plat
    plat.loop.close()


def sdk_transport(p):
    """C09 SDK transport that talks to the in-process resolver as the pilot agent."""

    def call(url: str, headers: dict[str, str]):
        path = url.split("http://resolver", 1)[1]
        return p.run(asgi(p.resolver_http, "GET", path,
                          headers={**headers, "X-Dev-User": f"agent:{AGENT}", "X-Dev-Kind": "service"}))

    return call


# 1 ----------------------------------------------------------------------------------------------
def test_01_seed_and_resolve_each_profile(p):
    expected = {"sales": ("Sales profile assistant", "opportunity-scan"),
                "service": ("Service profile assistant", "case-triage"),
                "risk": ("Risk profile assistant", "kyc-summary")}
    for bu, (persona, skill) in expected.items():
        status, r = p.resolve(bu)
        assert status == 200, r
        assert r["agent"]["id"] == AGENT
        assert r["profile"]["persona"]["name"] == persona
        assert [s["name"] for s in r["bindings"]["skills"]] == [skill]
        assert r["bindings"]["model"]["tier"] == "standard"
        assert {"agent", "profile", "guardrailPolicy", "modelPolicy", "binding.skill"} <= set(r["versions"])
        assert r["versions"]["profile"] == p.latest("profile", f"{AGENT}-{bu}")["id"]
        assert r["source"] == "registry"
    assert p.resolve("risk")[1]["thresholds"]["values"] == {"maxExposureEur": 250000}
    assert p.resolve("sales")[1]["source"] == "cache"
    # C07-F5: an agent reads only its own configuration
    assert p.resolve("sales", agent_identity="agent:someone-else")[0] == 403
    # every production read is logged (spec 11, audit)
    n = p.run(p.db.call(lambda tx: tx.scalar("SELECT COUNT(*) FROM config_read_log WHERE environment = 'production'")))
    assert n >= 4


# 2 ----------------------------------------------------------------------------------------------
def test_02_bu_persona_change_reaches_next_session(p):
    pinned = AgentConfig.load(AGENT, "sales", "production", resolver_url="http://resolver", transport=sdk_transport(p))
    assert pinned.persona["name"] == "Sales profile assistant"

    def change(c):
        c["persona"] = {"name": "Sales coach", "tone": "concise"}

    status, draft = p.draft("profile", f"{AGENT}-sales", change, user="sally@example.com")
    assert status == 201, draft
    status, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "Shorter, coaching tone",
                                                    "environment": "dev"}, user="sally@example.com")
    assert status == 201, cr
    assert cr["state"] == "awaiting_approval"
    assert [s["step"] for s in cr["steps"] if s["result"] == "passed"] == ["schema", "policy", "evaluation"]
    assert [r["role"] for r in cr["required_roles"]] == ["bu_owner"]  # a tone change: BU owner only
    # no self-approval (C05-F3)
    assert p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
                  user="sally@example.com")[0] == 403
    status, cr = p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
                        user="bob@example.com")
    assert status == 200 and cr["state"] == "activated", cr
    for env in ("test", "production"):  # dev -> test -> production (C05-F5)
        status, cr = p.call("POST", f"/change-requests/{cr['id']}/promote", {"environment": env},
                            user="sally@example.com")
        assert status == 200, cr

    nxt = AgentConfig.load(AGENT, "sales", "production", resolver_url="http://resolver", transport=sdk_transport(p))
    assert nxt.persona == {"name": "Sales coach", "tone": "concise"}
    assert nxt.versions["profile"] == draft["id"]
    assert nxt.otel_attributes()["eap.version.profile"] == draft["id"]
    assert pinned.persona["name"] == "Sales profile assistant"  # the running session stays pinned (C09-F1)
    # C03-F7 independent audit copy of the activated version
    assert any(draft["id"] in k for k in p.exporter.objects if k.startswith("production/"))


# 3 ----------------------------------------------------------------------------------------------
def test_03_threshold_beyond_guardrail_limit_is_rejected(p):
    status, body = p.draft("binding", f"{AGENT}-risk-thresholds",
                           lambda c: c["values"].update(maxExposureEur=2_000_000), user="rob@example.com")
    assert status == 422
    assert body["details"][0]["code"] == "limit"
    assert "exceeds the guardrail maximum 1000000" in body["details"][0]["message"]


# 4 ----------------------------------------------------------------------------------------------
def test_04_threshold_within_limits_routes_to_risk(p):
    status, draft = p.draft("binding", f"{AGENT}-risk-thresholds",
                            lambda c: c["values"].update(maxExposureEur=300_000), user="rob@example.com")
    assert status == 201, draft
    status, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "Raise limit for Q1",
                                                    "environment": "dev"}, user="rob@example.com")
    assert cr["state"] == "awaiting_approval"
    assert [r["role"] for r in cr["required_roles"]] == ["risk"]
    assert cr["required_roles"][0]["eligible"] == ["rita@example.com"]
    assert p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "bu_owner", "decision": "approve"},
                  user="bob@example.com")[0] == 403
    inbox = p.call("GET", "/change-requests?state=awaiting_approval&role=risk", user="rita@example.com")[1]["items"]
    assert [i["id"] for i in inbox] == [cr["id"]]
    status, cr = p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "risk", "decision": "approve"},
                        user="rita@example.com")
    assert cr["state"] == "activated"
    assert p.resolve("risk", env="dev")[1]["thresholds"]["values"]["maxExposureEur"] == 300_000


# 5 ----------------------------------------------------------------------------------------------
def test_05_failing_change_can_never_be_activated(p):
    status, draft = p.draft("profile", f"{AGENT}-sales",
                            lambda c: c.update(instructions="We guarantee a 10% return. " + c["instructions"]),
                            user="sally@example.com")
    assert status == 201
    status, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "promise returns",
                                                    "environment": "dev"}, user="sally@example.com")
    assert cr["state"] == "failed"
    ev = next(s for s in cr["steps"] if s["step"] == "evaluation")
    assert ev["result"] == "failed" and "suite pass rate" in ev["detail"]["targets"][0]["reasons"][0]
    for user in ("bob@example.com", "rita@example.com", "pat@example.com"):
        for role in ("bu_owner", "risk", "platform"):
            assert p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": role, "decision": "approve"},
                          user=user)[0] in (403, 409)
    with pytest.raises(store.Forbidden):
        p.run(p.engine.activate(cr["id"], "admin@example.com", "dev"))
    assert p.resolve("sales", env="dev")[1]["versions"]["profile"] != draft["id"]


def test_05b_policy_check_blocks_personal_data(p):
    _, draft = p.draft("profile", f"{AGENT}-sales",
                       lambda c: c.update(instructions="Pay into NL91ABNA0417164300 first."), user="sally@example.com")
    _, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "x", "environment": "dev"},
                   user="sally@example.com")
    assert cr["state"] == "failed"
    assert next(s for s in cr["steps"] if s["step"] == "policy")["result"] == "failed"


# 6 ----------------------------------------------------------------------------------------------
def test_06_update_on_versions_is_refused_sqlite(p):
    vid = p.latest("profile", f"{AGENT}-sales")["id"]
    for sql in ("UPDATE artifact_version SET content = '{}' WHERE id = ?", "DELETE FROM artifact_version WHERE id = ?"):
        with pytest.raises(Exception, match="append-only"):
            p.run(p.db.call(lambda tx, sql=sql: tx.execute(sql, (vid,))))


def _pg_available() -> bool:
    return bool(shutil.which("psql")) and subprocess.run(
        ["su", os.environ.get("EAP_TEST_PG_SUPERUSER", "postgres"), "-c", "psql -tAc 'select 1'"],
        capture_output=True, text=True).returncode == 0


@pytest.mark.skipif(not _pg_available(), reason="no local PostgreSQL")
def test_06_update_on_versions_is_refused_postgres():
    su = os.environ.get("EAP_TEST_PG_SUPERUSER", "postgres")
    ddl = MIGRATIONS_DIR / "001_registry.pg.sql"

    def psql(sql: str, db: str = "eap_e2e_06") -> subprocess.CompletedProcess:
        return subprocess.run(["su", su, "-c", f"psql -v ON_ERROR_STOP=1 -q -d {db}"], input=sql,
                              capture_output=True, text=True)

    subprocess.run(["su", su, "-c", "psql -q -c 'DROP DATABASE IF EXISTS eap_e2e_06' -c 'CREATE DATABASE eap_e2e_06'"],
                   check=True, capture_output=True)
    assert psql(ddl.read_text()).returncode == 0
    setup = ("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='eap_e2e_app') THEN "
             "CREATE ROLE eap_e2e_app LOGIN IN ROLE registry_app; END IF; END $$;\n"
             "SET search_path TO registry;\n"
             "INSERT INTO artifact (id,type,name,owner,created_at) VALUES ('a1','agent','x','o','t');\n"
             "INSERT INTO artifact_version (id,artifact_id,version,schema_version,content,content_hash,author,created_at) "
             f"VALUES ('v1','a1',1,'1.0.0','{{}}','{'a' * 64}','me','t');\n")
    assert psql(setup).returncode == 0
    as_app = psql("SET ROLE eap_e2e_app; SET search_path TO registry; UPDATE artifact_version SET author='x';")
    assert as_app.returncode != 0 and "permission denied" in as_app.stderr
    as_admin = psql("SET search_path TO registry; UPDATE artifact_version SET author='x';")
    assert as_admin.returncode != 0 and "append-only" in as_admin.stderr


# 7 ----------------------------------------------------------------------------------------------
def test_07_database_down_resolver_serves_last_known_good(p):
    assert p.resolve("sales")[1]["source"] == "registry"
    p.resolver.cache.drop_prefix("")  # force the next read past the cache
    p.db.close()                     # "stop the database"
    status, r = p.resolve("sales")
    assert status == 200 and r["source"] == "last-known-good"
    assert r["profile"]["persona"]["name"] == "Sales profile assistant"
    cfg = AgentConfig.load(AGENT, "sales", "production", resolver_url="http://resolver", transport=sdk_transport(p))
    assert cfg.source == "last-known-good"


def test_07b_sdk_last_known_good_when_resolver_down(p, tmp_path):
    AgentConfig.load(AGENT, "risk", "production", resolver_url="http://resolver", transport=sdk_transport(p),
                     cache_dir=str(tmp_path))

    def down(url, headers):
        raise OSError("connection refused")

    cfg = AgentConfig.load(AGENT, "risk", "production", resolver_url="http://resolver", transport=down,
                           cache_dir=str(tmp_path))
    assert cfg.source == "sdk-last-known-good" and cfg.thresholds["maxExposureEur"] == 250000


# 8 ----------------------------------------------------------------------------------------------
def test_08_cold_tool_binding_flows_through_provisioning(p):
    before = len(p.run(p.db.call(lambda tx: store.deployments(tx, "production"))))

    def add_tool(c):
        c["tools"].append({"capability": "exposure_check", "toolDefId": "exposure-check"})

    status, draft = p.draft("binding", f"{AGENT}-tools", add_tool, user="dora@example.com")
    assert status == 201, draft
    _, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "Risk needs exposure checks",
                                               "environment": "production"}, user="dora@example.com")
    assert cr["state"] == "awaiting_approval" and cr["cold"] is True
    assert [r["role"] for r in cr["required_roles"]] == ["security"]
    _, cr = p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "security", "decision": "approve"},
                   user="sam@example.com")
    assert cr["state"] == "activated", cr["steps"][-1]
    prov = [s for s in cr["steps"] if s["step"] == "provisioning"]
    assert [s["detail"]["environment"] for s in prov] == ["dev", "test", "production"]  # C11-F2 order
    deps = p.run(p.db.call(lambda tx: store.deployments(tx, "production")))
    assert len(deps) > before and deps[0]["platform"] == "mock" and deps[0]["status"] == "succeeded"
    state = p.run(p.adapter.read_state(AGENT, "production"))
    assert f"tool/{AGENT}/exposure-check" in state.resources
    assert [t["toolDefId"] for t in p.resolve("risk")[1]["bindings"]["tools"]] == ["crm-lookup", "exposure-check"]


def test_08b_failed_provisioning_rolls_back(p):
    p.adapter.fail_keys = {f"tool/{AGENT}/exposure-check"}
    before = p.run(p.adapter.read_state(AGENT, "dev")).fingerprint
    _, draft = p.draft("binding", f"{AGENT}-tools",
                       lambda c: c["tools"].append({"capability": "x", "toolDefId": "exposure-check"}),
                       user="dora@example.com")
    _, cr = p.call("POST", "/change-requests", {"versionIds": [draft["id"]], "reason": "r", "environment": "production"},
                   user="dora@example.com")
    _, cr = p.call("POST", f"/change-requests/{cr['id']}/approvals", {"role": "security", "decision": "approve"},
                   user="sam@example.com")
    assert cr["state"] == "failed"
    assert p.run(p.adapter.read_state(AGENT, "dev")).fingerprint == before  # C11-F4
    assert p.resolve("risk")[1]["versions"]["binding.tool"] != draft["id"]  # hot side never activated (C11-F3)


# 9 ----------------------------------------------------------------------------------------------
def test_09_pilot_recreated_from_registry(p):
    original = p.run(p.adapter.read_state(AGENT, "production"))
    assert original.resources
    p.adapter.delete_agent(AGENT, "production")
    snap = p.run(p.db.call(lambda tx: store.snapshot(tx, "production")))
    assert p.run(drift(p.adapter, compile_spec(snap, AGENT), "production"))  # C10-F4 drift reported
    status, out = p.call("POST", "/internal/v1/reconcile", {"agentId": AGENT, "environment": "production"})
    assert status == 200 and out["status"] == "succeeded"
    assert p.run(p.adapter.read_state(AGENT, "production")).fingerprint == original.fingerprint
    assert p.run(drift(p.adapter, compile_spec(snap, AGENT), "production")) == []
    again = p.run(p.provisioner.reconcile(AGENT, "production"))  # C10-F3 idempotent
    assert again["applied"] == []


# 10 ---------------------------------------------------------------------------------------------
def test_10_domain_mcp_tool_blocks_above_threshold(p):
    import server as template  # templates/mcp-server/server.py
    from eap_mcp_kit import Caller, ResolvedThresholds, ThresholdSource, caller
    from mcp.server.mcpserver.exceptions import ToolError

    mcp_service = Principal(sub="exposure-mcp@svc", email="exposure-mcp@svc", kind="service")
    p.resolver.identities.trusted.add("exposure-mcp@svc")

    async def via_resolver(c: Caller):  # the server reads active thresholds through C07 (C12-F2)
        r = await p.resolver.resolve(mcp_service, c.agent_id, c.business_unit, c.environment, on_behalf_of=c.agent_id)
        return ResolvedThresholds(r["thresholds"]["values"], r["versions"])

    server = template.build(ThresholdSource(via_resolver))

    async def call(amount):
        with caller(Caller(AGENT, "risk", "production", on_behalf_of="analyst@example.com")):
            return await server.mcp.call_tool("check_exposure", {"customer_id": "C-1001", "amount_eur": amount})

    assert not getattr(p.run(call(200_000)), "isError", False)
    with pytest.raises(ToolError, match="blocked"):
        p.run(call(300_000))  # above maxExposureEur=250000, whatever the agent's instructions said
    assert server.blocked[-1] == {"tool": "check_exposure", "agent": AGENT, "bu": "risk", "key": "maxExposureEur",
                                  "value": 300_000.0, "limit": 250000}
