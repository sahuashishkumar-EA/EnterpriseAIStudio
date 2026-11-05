"""Seed the example agents end-to-end through the public APIs (dev auth):

  ingest knowledge -> create agent -> eval suite -> run evals -> approve -> publish -> alias prod

Usage (docker-compose / local):
  python examples/seed.py --governor http://localhost:8080 --runtime http://localhost:8081
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).parent

ADMIN = {"X-Dev-User": "admin@example.com", "X-Dev-Roles": "*:platform_admin"}
AUTHOR = {"X-Dev-User": "alice@example.com", "X-Dev-Roles": "retail:author,customer-data:author,platform-quality:author"}
APPROVER = {"X-Dev-User": "bob@example.com", "X-Dev-Roles": "*:approver"}
PUBLISHER = {"X-Dev-User": "carol@example.com", "X-Dev-Roles": "*:publisher"}

SUITES: dict[str, dict[str, Any]] = {
    "entity-resolver": {
        "file": "entity-resolver.csv",
        "assertions": [
            {"type": "status", "equals": "ok"},
            {"type": "equals", "path": "output.customer_id", "value_from": "expected.customer_id"},
            {"type": "citations", "min": 1},
        ],
    },
    "evaluator": {
        "file": "evaluator.json",
        "assertions": [
            {"type": "status", "equals": "ok"},
            {"type": "llm_judge", "rubric": "The output contains a numeric score and a pass/fail verdict.", "threshold": 0.5},
        ],
    },
    "customer-profile": {
        "file": "customer-profile.jsonl",
        "assertions": [{"type": "status", "equals": "ok"}, {"type": "max_cost_usd", "max": 0.5}],
    },
}
ORDER = ["entity-resolver", "evaluator", "customer-profile"]


def _check(r: httpx.Response, what: str) -> dict[str, Any]:
    if r.status_code >= 400:
        raise RuntimeError(f"{what} failed: {r.status_code} {r.text[:800]}")
    return r.json() if r.content else {}


async def ingest_knowledge(rt: httpx.AsyncClient) -> None:
    docs = [json.loads(line) for line in (HERE / "knowledge" / "customer-master.jsonl").read_text().splitlines() if line.strip()]
    _check(await rt.post("/v1/knowledge/customer-master/documents", json={"documents": docs, "embedding_model": "mock-embedding"},
                         headers=ADMIN), "ingest customer-master")


async def release_agent(gov: httpx.AsyncClient, name: str, alias: str = "prod") -> dict[str, Any]:
    definition = json.loads((HERE / "agents" / f"{name}.json").read_text())
    r = await gov.get(f"/api/v1/agents/{name}", headers=AUTHOR)
    if r.status_code == 404:
        _check(await gov.post("/api/v1/agents", json={"name": name, "owner_bu": definition["owner_bu"],
                                                       "description": definition["description"], "definition": definition},
                              headers=AUTHOR), f"create {name}")
    else:
        _check(await gov.put(f"/api/v1/agents/{name}/draft", json={"definition": definition}, headers=AUTHOR), f"draft {name}")
    s = SUITES[name]
    path = HERE / "datasets" / s["file"]
    _check(await gov.post(f"/api/v1/agents/{name}/evals/suites", json={
        "name": "regression", "content": path.read_text(), "format": path.suffix.lstrip("."),
        "assertions": s["assertions"], "required": True}, headers=AUTHOR), f"suite {name}")
    runs = _check(await gov.post(f"/api/v1/agents/{name}/evals/run?wait=true", json={"target": "draft"}, headers=AUTHOR),
                  f"evals {name}")["runs"]
    for run in runs:
        if run["pass_rate"] < 1.0:
            raise RuntimeError(f"evals for {name} failed: {json.dumps(run['results'], indent=1)[:2000]}")
    check = _check(await gov.get(f"/api/v1/agents/{name}/release-check", headers=APPROVER), "release check")
    _check(await gov.post(f"/api/v1/agents/{name}/approvals", json={"decision": "approve", "comment": "seed",
                                                                     "content_hash": check["content_hash"]},
                          headers=APPROVER), f"approve {name}")
    return _check(await gov.post(f"/api/v1/agents/{name}/publish", json={"changelog": "seeded example", "alias": alias},
                                 headers=PUBLISHER), f"publish {name}")


async def seed(gov: httpx.AsyncClient, rt: httpx.AsyncClient) -> dict[str, Any]:
    await ingest_knowledge(rt)
    return {name: await release_agent(gov, name) for name in ORDER}


async def seed_studio_example(gov: httpx.AsyncClient, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Create the two-context bank-assistant and quick-publish it (governor must run in dev mode)."""
    headers = headers or ADMIN
    definition = json.loads((HERE / "studio" / "bank-assistant.json").read_text())
    r = await gov.get("/api/v1/agents/bank-assistant", headers=headers)
    if r.status_code == 404:
        _check(await gov.post("/api/v1/agents", json={"name": "bank-assistant", "owner_bu": definition["owner_bu"],
                                                       "description": definition["description"], "definition": definition},
                              headers=headers), "create bank-assistant")
    return _check(await gov.post("/api/v1/agents/bank-assistant/quick-publish", json={}, headers=headers), "quick publish")


async def _main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--governor", default="http://localhost:8080")
    ap.add_argument("--runtime", default="http://localhost:8081")
    ap.add_argument("--studio", action="store_true", help="also create the two-context bank-assistant (dev mode)")
    a = ap.parse_args()
    async with httpx.AsyncClient(base_url=a.governor, timeout=120) as gov, \
            httpx.AsyncClient(base_url=a.runtime, timeout=120) as rt:
        out = await seed(gov, rt)
        if a.studio:
            out["bank-assistant"] = await seed_studio_example(gov)
    print(json.dumps({k: v["version"] for k, v in out.items()}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
