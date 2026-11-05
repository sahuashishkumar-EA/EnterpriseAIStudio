"""Release gate: draft -> validate -> eval -> approve -> publish.

Publishing a draft requires, for the draft's *current content hash*:
  1. schema + policy + catalog validation passes (no violations)
  2. no agent-call cycle through currently aliased versions
  3. every required eval suite has a completed run on this hash with
     pass_rate >= policy.release.min_eval_pass_rate (if require_eval)
  4. >= min_approvals distinct approvals on this hash, no rejection, and under
     separation of duties none of the approvers edited the draft or is publishing it
  5. the version bump is at least what the change requires (breaking => major)

Evals and approvals are bound to the content hash, so any edit after approval
invalidates them automatically.
"""

from __future__ import annotations

from typing import Any

from eap_core import schema
from eap_core.db import Tx, dumps, loads, new_id, now_iso
from eap_core.policy import check_definition

from . import store
from .store import Conflict, Invalid


def validate(tx: Tx, defn: schema.AgentDefinition, owner_bu: str) -> dict[str, Any]:
    policy = store.effective_policy(tx, owner_bu)
    catalog = store.catalog_subset(tx, defn)
    violations = [v.model_dump() for v in check_definition(defn, policy, catalog)]
    for i, b in enumerate(defn.bindings.agents):
        try:
            store.get_agent(tx, b.name)
        except store.NotFound:
            violations.append({"code": "agent_unknown", "path": f"bindings.agents[{i}]", "message": f"agent {b.name} not found"})
            continue
        if schema.SEMVER_RE.match(b.alias):
            target = b.alias
        else:
            a = store.get_alias(tx, b.name, b.alias)
            target = a["version"] if a else None
        if target is None:
            violations.append({"code": "alias_unknown", "path": f"bindings.agents[{i}].alias",
                               "message": f"{b.name}@{b.alias} does not exist"})
            continue
        try:
            v = store.get_version(tx, b.name, target)
        except store.NotFound:
            violations.append({"code": "version_unknown", "path": f"bindings.agents[{i}]", "message": f"{b.name}@{target} not found"})
            continue
        if not v["definition"].get("exposure", {}).get("callable_by_agents", True):
            violations.append({"code": "not_callable", "path": f"bindings.agents[{i}]",
                               "message": f"{b.name} is not callable by agents"})
    cycle = store.find_static_cycle(tx, defn)
    if cycle:
        violations.append({"code": "cycle", "path": "bindings.agents", "message": "call cycle: " + " -> ".join(cycle)})
    return {"ok": not violations, "violations": violations, "content_hash": defn.content_hash()}


def release_check(tx: Tx, name: str, publisher: str | None = None, bump: str | None = None,
                  relaxed: bool = False) -> dict[str, Any]:
    """relaxed (dev environments only): separation of duties is not enforced, so one person
    may author, approve and publish. Every step is still recorded in the audit log."""
    agent = store.get_agent(tx, name)
    draft = store.get_draft(tx, name)
    defn = schema.parse_definition(draft["definition"])
    h = draft["content_hash"]
    policy = store.effective_policy(tx, agent["owner_bu"])
    rp = policy.release
    if relaxed:
        rp = rp.model_copy(update={"separation_of_duties": False})
    blockers: list[str] = []

    val = validate(tx, defn, agent["owner_bu"])
    if not val["ok"]:
        blockers.append(f"validation: {len(val['violations'])} violation(s)")

    # ---------------------------------------------------------------- evals
    suites = tx.fetchall("SELECT id, name, required FROM eval_suites WHERE agent_name = ? ORDER BY name", (name,))
    suite_status = []
    eval_run_ids: list[str] = []
    for s in suites:
        run = tx.fetchone(
            "SELECT id, status, pass_rate, passed, total FROM eval_runs WHERE agent_name = ? AND suite_id = ? "
            "AND content_hash = ? AND status = 'completed' ORDER BY started_at DESC, id DESC LIMIT 1",
            (name, s["id"], h),
        )
        passed = bool(run) and run["pass_rate"] >= rp.min_eval_pass_rate
        suite_status.append({"suite": s["name"], "suite_id": s["id"], "required": bool(s["required"]),
                             "run": run, "passed": passed})
        if run:
            eval_run_ids.append(run["id"])
        if rp.require_eval and s["required"] and not passed:
            blockers.append(
                f"eval: suite {s['name']} has no passing run on this content hash"
                f" (need pass_rate >= {rp.min_eval_pass_rate})"
            )
    if rp.require_eval and not any(s["required"] for s in suites):
        blockers.append("eval: no required eval suite defined")

    # ------------------------------------------------------------ approvals
    rows = tx.fetchall(
        "SELECT approver, decision, comment, at FROM approvals WHERE agent_name = ? AND content_hash = ?", (name, h)
    )
    excluded = set(draft["editors"]) | ({publisher} if publisher else set())
    rejects = [r for r in rows if r["decision"] == "reject"]
    valid = sorted(
        {r["approver"] for r in rows if r["decision"] == "approve" and (not rp.separation_of_duties or r["approver"] not in excluded)}
    )
    if rejects:
        blockers.append(f"approval: rejected by {', '.join(r['approver'] for r in rejects)}")
    if len(valid) < rp.min_approvals:
        msg = f"approval: {len(valid)}/{rp.min_approvals} valid approvals"
        if rp.separation_of_duties:
            msg += " (editors and the publisher cannot approve their own change)"
        blockers.append(msg)

    # --------------------------------------------------------------- semver
    latest = store.latest_version(tx, name)
    prev = schema.parse_definition(store.get_version(tx, name, latest)["definition"]) if latest else None
    breaking = schema.breaking_changes(prev, defn) if prev else []
    required = schema.required_bump(prev, defn)
    if prev is not None and prev.content_hash() == h:
        blockers.append(f"draft is identical to the latest version {latest}")
    chosen = bump or required
    if chosen not in schema.BUMP_ORDER:
        raise Invalid("bump must be major, minor or patch")
    if prev is not None and schema.BUMP_ORDER[chosen] < schema.BUMP_ORDER[required]:
        blockers.append(f"semver: change requires a {required} bump (breaking: {breaking or 'none'})")
    next_version = schema.bump(latest, chosen) if latest else "1.0.0"

    return {
        "ok": not blockers,
        "agent": name,
        "content_hash": h,
        "blockers": blockers,
        "validation": val,
        "evals": suite_status,
        "eval_run_ids": eval_run_ids,
        "approvals": {"valid": valid, "all": rows, "required": rp.min_approvals,
                      "separation_of_duties": rp.separation_of_duties, "excluded": sorted(excluded)},
        "latest_version": latest,
        "breaking_changes": breaking,
        "required_bump": required,
        "bump": chosen,
        "next_version": next_version,
        "policy": rp.model_dump(),
        "relaxed": relaxed,
    }


def approve(tx: Tx, name: str, actor: str, decision: str, comment: str, content_hash: str | None,
            relaxed: bool = False) -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise Invalid("decision must be approve or reject")
    draft = store.get_draft(tx, name)
    if content_hash and content_hash != draft["content_hash"]:
        raise Conflict("draft changed since you reviewed it", {"current": draft["content_hash"]})
    agent = store.get_agent(tx, name)
    policy = store.effective_policy(tx, agent["owner_bu"])
    if policy.release.separation_of_duties and not relaxed and actor in draft["editors"]:
        raise Conflict("separation of duties: you edited this draft and cannot approve it")
    tx.execute(
        "INSERT INTO approvals (id, agent_name, content_hash, approver, decision, comment, at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (agent_name, content_hash, approver) DO UPDATE SET decision = excluded.decision, "
        "comment = excluded.comment, at = excluded.at",
        (new_id("apr_"), name, draft["content_hash"], actor, decision, comment, now_iso()),
    )
    store.audit(tx, actor, f"draft.{decision}", f"agent/{name}", {"content_hash": draft["content_hash"], "comment": comment})
    return {"content_hash": draft["content_hash"], "decision": decision}


def publish(tx: Tx, name: str, actor: str, bump: str | None, changelog: str, relaxed: bool = False) -> dict[str, Any]:
    check = release_check(tx, name, publisher=actor, bump=bump, relaxed=relaxed)
    if not check["ok"]:
        raise Conflict("release gate failed", check)
    draft = store.get_draft(tx, name)
    defn = schema.parse_definition(draft["definition"])
    version = check["next_version"]
    store.insert_version(
        tx, defn, version, actor, changelog, check["breaking_changes"], check["eval_run_ids"], check["approvals"]["valid"]
    )
    tx.execute("UPDATE drafts SET editors = ? WHERE agent_name = ?", (dumps([]), name))
    store.audit(
        tx,
        actor,
        "version.publish",
        f"agent/{name}/{version}",
        {"content_hash": defn.content_hash(), "bump": check["bump"], "breaking": check["breaking_changes"],
         "approvals": check["approvals"]["valid"], "eval_run_ids": check["eval_run_ids"]},
    )
    return {"agent": name, "version": version, "content_hash": defn.content_hash(), "check": check}


def record_eval_run(tx: Tx, run: dict[str, Any]) -> None:
    tx.execute(
        "UPDATE eval_runs SET status = ?, passed = ?, total = ?, pass_rate = ?, cost_usd = ?, results = ?, error = ?, "
        "finished_at = ? WHERE id = ?",
        (run["status"], run["passed"], run["total"], run["pass_rate"], run["cost_usd"], dumps(run["results"]),
         run.get("error"), now_iso(), run["id"]),
    )


def load_eval_run(tx: Tx, run_id: str) -> dict[str, Any]:
    r = tx.fetchone("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
    if r is None:
        raise store.NotFound("eval run not found")
    r["results"] = loads(r["results"], [])
    return r
