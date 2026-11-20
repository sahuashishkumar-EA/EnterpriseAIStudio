#!/usr/bin/env python3
"""Upload eval suites, run them on an agent's draft, and (optionally) release it to an alias.

    # 1. upload + run (author)
    python scripts/eval-release.py bank-assistant examples/evals/bank-assistant

    # 2. release: approve as a second person, publish, point prod
    python scripts/eval-release.py bank-assistant examples/evals/bank-assistant --release \
        --approver tom@example.com

A suite directory holds one dataset per suite (<name>.jsonl / .json / .csv) and an optional
suites.json: {"<name>": {"required": bool, "assertions": [suite-level assertions]}}.

Auth: locally the dev headers (--user / --roles). Against a deployed governor pass
--token "$(gcloud auth print-identity-token)" (or set EAP_TOKEN) and run each step as the person
who holds that role; on GCP the console (Tests / Release tabs) does the same steps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def headers(user: str, roles: str, token: str) -> dict[str, str]:
    if token:
        return {"Authorization": f"Bearer {token}"}
    h = {"X-Dev-User": user}
    if roles:
        h["X-Dev-Roles"] = roles
    return h


def check(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        sys.exit(f"{what} failed ({r.status_code}): {r.text[:800]}")
    return r.json()


def upload(g: httpx.Client, agent: str, folder: Path, h: dict[str, str]) -> list[str]:  # returns suite ids
    manifest = json.loads((folder / "suites.json").read_text()) if (folder / "suites.json").exists() else {}
    ids = []
    for f in sorted(folder.iterdir()):
        if f.suffix not in (".jsonl", ".json", ".csv") or f.name == "suites.json":
            continue
        meta = manifest.get(f.stem, {})
        body = {"name": f.stem, "content": f.read_text(), "filename": f.name,
                "assertions": meta.get("assertions", []), "required": meta.get("required", True)}
        s = check(g.post(f"/api/v1/agents/{agent}/evals/suites", json=body, headers=h), f"upload {f.name}")
        sid = s.get("id") or s.get("suite", {}).get("id")
        ids.append(sid)
        print(f"  uploaded {f.stem:<12} {'required' if body['required'] else 'optional':<9} {sid}")
    return ids


def run(g: httpx.Client, agent: str, suite_ids: list[str], h: dict[str, str], version: str | None) -> bool:
    body = {"target": "version", "version": version} if version else {"target": "draft"}
    if suite_ids:
        body["suite_ids"] = suite_ids
    runs = check(g.post(f"/api/v1/agents/{agent}/evals/run?wait=true", json=body, headers=h, timeout=900), "run")["runs"]
    suites = {s["id"]: s for s in check(g.get(f"/api/v1/agents/{agent}/evals/suites", headers=h), "suites")["suites"]}
    all_required_ok = True
    for r in runs:
        s = suites.get(r["suite_id"], {})
        req = bool(s.get("required"))
        ok = r["status"] == "completed" and r["pass_rate"] >= 1.0
        all_required_ok &= ok or not req
        print(f"\n{'PASS' if ok else 'FAIL'}  {s.get('name', r['suite_id'])} ({'required' if req else 'optional'}): "
              f"{r['passed']}/{r['total']}  cost ${r.get('cost_usd') or 0:.4f}" + (f"  error: {r['error']}" if r.get("error") else ""))
        for c in r.get("results") or []:
            if c["passed"]:
                continue
            print(f"   x {c['case_id']}  status={c['status']}" + (f"  error={c['error']}" if c.get("error") else ""))
            for a in c["assertions"]:
                if not a["passed"]:
                    print(f"       - {a['type']} {a.get('path') or ''}: {a['detail']}")
            out = c.get("output")
            if out:
                print(f"       output: {str(out)[:200]!r}")
    return all_required_ok


def release(g: httpx.Client, agent: str, alias: str, author: dict[str, str], approver: dict[str, str],
            changelog: str) -> None:
    rc = check(g.get(f"/api/v1/agents/{agent}/release-check", headers=author), "release check")
    blockers = [b for b in rc["blockers"] if not b.startswith("approval")]
    if blockers:
        sys.exit("cannot release yet:\n  - " + "\n  - ".join(rc["blockers"]))
    if not rc["approvals"]["valid"]:
        check(g.post(f"/api/v1/agents/{agent}/approvals", headers=approver,
                     json={"decision": "approve", "comment": "evals passed", "content_hash": rc["content_hash"]}), "approve")
        print(f"  approved {rc['content_hash'][:19]}…")
    pub = check(g.post(f"/api/v1/agents/{agent}/publish", headers=approver,
                       json={"changelog": changelog, "alias": alias}), "publish")
    print(f"  published {agent} {pub['version']} -> {alias}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agent")
    ap.add_argument("suites", nargs="?", help="folder with suite datasets (omit to run the suites already uploaded)")
    ap.add_argument("--governor", default=os.environ.get("EAP_GOVERNOR_URL", "http://localhost:8080"))
    ap.add_argument("--user", default="admin@example.com", help="dev mode: who uploads and runs (author)")
    ap.add_argument("--roles", default="*:platform_admin", help="dev mode: roles of --user (empty = from the directory)")
    ap.add_argument("--token", default=os.environ.get("EAP_TOKEN", ""), help="bearer token for a deployed governor")
    ap.add_argument("--version", help="test a published version instead of the draft")
    ap.add_argument("--release", action="store_true", help="after passing: approve, publish and point --alias")
    ap.add_argument("--alias", default="prod")
    ap.add_argument("--approver", default="", help="dev mode: approver/publisher (must not have edited the draft)")
    ap.add_argument("--approver-roles", default="", help="dev mode: roles of --approver (empty = from the directory)")
    ap.add_argument("--changelog", default="released after evals")
    a = ap.parse_args()

    author = headers(a.user, a.roles, a.token)
    approver = headers(a.approver, a.approver_roles, "") if a.approver else author
    with httpx.Client(base_url=a.governor, timeout=60) as g:
        if a.suites:
            print(f"Uploading suites from {a.suites}")
            upload(g, a.agent, Path(a.suites), author)
        print(f"Running {'version ' + a.version if a.version else 'the draft'} of {a.agent}")
        ok = run(g, a.agent, [], author, a.version)  # all suites: the release gate needs every required one
        if not ok:
            sys.exit("\nRequired suites failed: fix the agent (or the test) and run again.")
        print("\nAll required suites passed.")
        if a.release:
            if a.version:
                sys.exit("--release works on the draft; to move an alias to an existing version use the console")
            release(g, a.agent, a.alias, author, approver, a.changelog)


if __name__ == "__main__":
    main()
