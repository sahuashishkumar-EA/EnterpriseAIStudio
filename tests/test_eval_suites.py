"""The example eval suites parse, and the guardrails suite passes on the mock model (code-enforced controls)."""

import json
from pathlib import Path

from eap_governor import evals
from examples.seed import HERE
from tests.harness import build

ADMIN = {"X-Dev-User": "admin@example.com", "X-Dev-Roles": "*:platform_admin"}
SUITES = Path(__file__).resolve().parents[1] / "examples" / "evals" / "bank-assistant"


def test_example_suites_parse():
    files = sorted(SUITES.glob("*.json"))
    assert [f.stem for f in files] == ["answers", "guardrails", "quality"]
    for f in files:
        meta, name = json.loads(f.read_text()), f.stem
        assert meta["name"] == name and isinstance(meta["required"], bool)
        cases = evals.parse_dataset(f.read_text(), "json")
        assert cases and len({c["id"] for c in cases}) == len(cases)
        for a in [*meta.get("assertions", []), *(x for c in cases for x in c.get("assertions", []))]:
            evals.check_assertion_spec(a, name)


def test_guardrails_suite_passes_on_mock():
    p = build(gov_overrides={"relaxed_release": True})
    definition = json.loads((HERE / "studio" / "bank-assistant.json").read_text())
    r = p.gov("POST", "/api/v1/agents", ADMIN, json={"name": "bank-assistant", "owner_bu": "retail", "definition": definition})
    assert r.status_code == 201, r.text
    text = (SUITES / "guardrails.json").read_text()
    meta = json.loads(text)
    s = p.gov("POST", "/api/v1/agents/bank-assistant/evals/suites", ADMIN, json={
        "name": meta["name"], "content": text, "format": "json", "assertions": meta["assertions"], "required": meta["required"]})
    assert s.status_code in (200, 201), s.text
    run = p.gov("POST", "/api/v1/agents/bank-assistant/evals/run?wait=true", ADMIN, json={"target": "draft"}).json()["runs"][0]
    failed = [(c["case_id"], [a for a in c["assertions"] if not a["passed"]]) for c in run["results"] if not c["passed"]]
    assert run["pass_rate"] == 1.0, failed


B2B = SUITES.parent / "b2b-account-profile"


def test_b2b_suites_parse():
    for f in sorted(B2B.glob("*.json")):
        meta = json.loads(f.read_text())
        assert meta["name"] == f.stem
        cases = evals.parse_dataset(f.read_text(), "json")
        assert len({c["id"] for c in cases}) == len(cases)
        for a in meta["assertions"]:
            evals.check_assertion_spec(a, f.stem)


def test_b2b_contract_and_guardrails_pass_on_mock():
    p = build(gov_overrides={"relaxed_release": True})
    d = json.loads((HERE / "agents" / "b2b-account-profile.json").read_text())
    r = p.gov("POST", "/api/v1/agents", ADMIN, json={"name": d["name"], "owner_bu": "retail", "definition": d})
    assert r.status_code == 201, r.text
    for name in ("contract", "guardrails"):
        text = (B2B / f"{name}.json").read_text()
        meta = json.loads(text)
        s = p.gov("POST", f"/api/v1/agents/{d['name']}/evals/suites", ADMIN, json={
            "name": name, "content": text, "format": "json", "assertions": meta["assertions"], "required": True})
        assert s.status_code in (200, 201), s.text
    for run in p.gov("POST", f"/api/v1/agents/{d['name']}/evals/run?wait=true", ADMIN, json={"target": "draft"}).json()["runs"]:
        failed = [(c["case_id"], [a for a in c["assertions"] if not a["passed"]]) for c in run["results"] if not c["passed"]]
        assert run["pass_rate"] == 1.0, failed


def test_account_lookup_and_phone_redaction():
    from eap_runtime.guardrails import PII_PATTERNS
    from eap_runtime.tools import account_lookup

    assert account_lookup("a-2004")["parent_account"] == "A-2003"
    assert account_lookup("contoso-manufacturing.example")["account_id"] == "A-2002"
    amb = account_lookup("Nordic")
    assert amb["found"] is False and [m["id"] for m in amb["matches"]] == ["A-2003", "A-2004"]
    assert account_lookup("Globex") == {"found": False, "searched_for": "Globex"}
    for phone in ("+358 9 555 0401", "+46 8 555 0301", "+31 20 555 0101"):
        assert PII_PATTERNS["phone"].search(f"call {phone} today").group(0) == phone
