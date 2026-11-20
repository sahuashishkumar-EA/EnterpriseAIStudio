"""The example eval suites parse, and the guardrails suite passes on the mock model (code-enforced controls)."""

import json
from pathlib import Path

from eap_governor import evals
from examples.seed import HERE
from tests.harness import build

ADMIN = {"X-Dev-User": "admin@example.com", "X-Dev-Roles": "*:platform_admin"}
SUITES = Path(__file__).resolve().parents[1] / "examples" / "evals" / "bank-assistant"


def test_example_suites_parse():
    manifest = json.loads((SUITES / "suites.json").read_text())
    for name, meta in manifest.items():
        cases = evals.parse_dataset((SUITES / f"{name}.jsonl").read_text(), "jsonl")
        assert cases and len({c["id"] for c in cases}) == len(cases)
        for a in [*meta.get("assertions", []), *(x for c in cases for x in c.get("assertions", []))]:
            evals.check_assertion_spec(a, name)


def test_guardrails_suite_passes_on_mock():
    p = build(gov_overrides={"relaxed_release": True})
    definition = json.loads((HERE / "studio" / "bank-assistant.json").read_text())
    r = p.gov("POST", "/api/v1/agents", ADMIN, json={"name": "bank-assistant", "owner_bu": "retail", "definition": definition})
    assert r.status_code == 201, r.text
    meta = json.loads((SUITES / "suites.json").read_text())["guardrails"]
    s = p.gov("POST", "/api/v1/agents/bank-assistant/evals/suites", ADMIN, json={
        "name": "guardrails", "content": (SUITES / "guardrails.jsonl").read_text(), "format": "jsonl", **meta})
    assert s.status_code in (200, 201), s.text
    run = p.gov("POST", "/api/v1/agents/bank-assistant/evals/run?wait=true", ADMIN, json={"target": "draft"}).json()["runs"][0]
    failed = [(c["case_id"], [a for a in c["assertions"] if not a["passed"]]) for c in run["results"] if not c["passed"]]
    assert run["pass_rate"] == 1.0, failed
