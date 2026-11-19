"""C05 Promotion workflow: validate -> policy -> evaluate -> approve -> activate.

A durable state machine on the registry database (ADR-003): every transition is a new
workflow_step row and the change request's state is the state of its latest step (ADR-004).
Each step is idempotent, so a Cloud Tasks retry after a crash simply re-runs it.

States: submitted -> checking -> awaiting_approval -> (provisioning ->) activated
        failed | rejected | activated_pending_review (emergency rollback, C05-F7)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from eap_core.db import Database, Tx, dumps, loads, new_id, now_iso
from eap_core.events import Event, Publisher
from eap_metamodel import Metamodel
from eap_store import store
from eap_store.store import Conflict, Forbidden, Invalid, NotFound

from .policy import LocalPolicy, PolicyEngine

log = logging.getLogger("eap.workflow")

ENV_ORDER = ("dev", "test", "production")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
TERMINAL = {"failed", "rejected"}


class Evaluations(Protocol):
    async def evaluate(self, version_ids: list[str], environment: str, change_request_id: str | None) -> dict[str, Any]: ...


@dataclass
class WorkflowSettings:
    # C05-F4: auto-approval is disabled until the owner of that policy decides (spec 12)
    auto_approval_enabled: bool = False
    auto_approve_risk_tiers: set[str] = field(default_factory=set)
    allow_bootstrap: bool = True


class PromotionEngine:
    def __init__(self, db: Database, mm: Metamodel, evaluations: Evaluations, publisher: Publisher,
                 exporter: Any, policy: PolicyEngine | None = None, tasks: Any = None,
                 settings: WorkflowSettings | None = None):
        self.db = db
        self.mm = mm
        self.evals = evaluations
        self.events = publisher
        self.exporter = exporter
        self.policy = policy or LocalPolicy()
        self.tasks = tasks
        self.settings = settings or WorkflowSettings()
        self.provisioner: Any = None  # set by Provisioner; used when no task queue is configured

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _step(tx: Tx, cr_id: str, step: str, result: str, state: str, detail: dict[str, Any] | None = None) -> None:
        tx.execute(
            "INSERT INTO workflow_step (id, change_request_id, step, result, state, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("ws_"), cr_id, step, result, state, dumps(detail or {}), now_iso()))

    @staticmethod
    def get_cr(tx: Tx, cr_id: str) -> dict[str, Any]:
        r = tx.fetchone("SELECT * FROM change_request WHERE id = ?", (cr_id,))
        if not r:
            raise NotFound(f"change request {cr_id} not found")
        steps = tx.fetchall("SELECT * FROM workflow_step WHERE change_request_id = ? ORDER BY seq", (cr_id,))
        approvals = tx.fetchall("SELECT * FROM approval WHERE change_request_id = ? ORDER BY seq", (cr_id,))
        evals = tx.fetchall("SELECT id, version_id, environment, passed, scores, at FROM evaluation_run "
                            "WHERE change_request_id = ? ORDER BY seq", (cr_id,))
        state = steps[-1]["state"] if steps else r["state"]
        routing = next((loads(s["detail"], {}) for s in reversed(steps) if s["step"] == "routing"), {})
        return {**r, "version_ids": loads(r["version_ids"], []), "state": state,
                "steps": [{**s, "detail": loads(s["detail"], {})} for s in steps],
                "approvals": approvals,
                "evaluations": [{**e, "scores": loads(e["scores"], {})} for e in evals],
                "required_roles": routing.get("required_roles", []),
                "cold": routing.get("cold", False)}

    async def _emit(self, event_type: str, cr: dict[str, Any], version_ids: list[str] | None = None,
                    environment: str | None = None, **extra: Any) -> None:
        def arts(tx: Tx):
            return {v: store.get_version(tx, v)["artifact_id"] for v in (version_ids or cr["version_ids"])}

        mapping = await self.db.call(arts)
        for vid, aid in mapping.items():
            await self.events.publish(Event(event_type=event_type, source="workflow", data={
                "artifact_id": aid, "version_id": vid, "environment": environment or cr["environment"],
                "change_request_id": cr["id"], **extra}))

    # ------------------------------------------------------------------ submit

    async def submit(self, version_ids: list[str], reason: str, environment: str, requested_by: str) -> dict[str, Any]:
        if environment not in ENV_ORDER:
            raise Invalid(f"unknown environment {environment!r}")
        if not version_ids:
            raise Invalid("versionIds is empty")
        if not reason.strip():
            raise Invalid("a reason is required (C02-F4)")

        def run(tx: Tx) -> str:
            tier = "low"
            for vid in version_ids:
                v = store.get_version(tx, vid)
                if v["status"] != "draft":
                    raise Conflict(f"version {vid} is {v['status']}, not a draft")
                latest = store.latest_version(tx, v["artifact_id"])
                if latest and latest["id"] != vid:
                    raise Conflict(f"version {vid} is not the latest draft of its artifact")
                t = self._risk_tier(tx, v)
                tier = t if RISK_ORDER[t] > RISK_ORDER[tier] else tier
            cr_id = new_id("cr_")
            tx.execute("INSERT INTO change_request (id, version_ids, reason, risk_tier, environment, requested_by, "
                       "created_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted')",
                       (cr_id, dumps(version_ids), reason, tier, environment, requested_by, now_iso()))
            for vid in version_ids:
                store.set_status(tx, vid, "in_review", requested_by, cr_id)
            self._step(tx, cr_id, "submitted", "info", "checking", {"requested_by": requested_by})
            store.audit(tx, requested_by, "change_request.submit", cr_id, None, {"versions": version_ids})
            return cr_id

        cr_id = await self.db.call(run)
        cr = await self.db.call(self.get_cr, cr_id)
        await self._emit("config.change.submitted", cr)
        if self.tasks is not None:
            await self.tasks.enqueue("/internal/v1/tasks/checks", {"change_request_id": cr_id}, task_id=f"checks-{cr_id}")
            return cr
        return await self.run_checks(cr_id)

    def _risk_tier(self, tx: Tx, v: dict[str, Any]) -> str:
        c = v["content"]
        art = store.get_artifact(tx, v["artifact_id"])
        if art["type"] == "agent":
            return c.get("riskTier", "medium")
        agent_id = c.get("agentId")
        if agent_id:
            a = store.find_artifact(tx, "agent", agent_id)
            av = a and store.latest_version(tx, a["id"])
            return (av and av["content"].get("riskTier")) or "medium"
        return "medium"  # shared artifacts affect several agents

    # ------------------------------------------------------------------ checks

    async def run_checks(self, cr_id: str) -> dict[str, Any]:
        cr = await self.db.call(self.get_cr, cr_id)
        if cr["state"] != "checking":
            return cr
        env = cr["environment"]

        # 1. schema validation (C03)
        def schema_step(tx: Tx):
            errors, versions = {}, []
            for vid in cr["version_ids"]:
                v = store.get_version(tx, vid)
                art = store.get_artifact(tx, v["artifact_id"])
                e = store.validate_content(tx, self.mm, art["type"], v["content"])
                if e:
                    errors[vid] = e
                base = store.active_version(tx, art["id"], env)
                if base is None and v["parent_version_id"]:
                    base = store.get_version(tx, v["parent_version_id"])
                cls = self.mm.classify(art["type"], base and base["content"], v["content"])
                versions.append({"versionId": vid, "artifactId": art["id"], "type": art["type"], "name": art["name"],
                                 "content": v["content"], "cold_paths": cls.cold, "hot_paths": cls.hot,
                                 "approvers": sorted(cls.approvers), "scopes": store.scopes_of(art, v["content"]),
                                 "author": v["author"]})
            return errors, versions

        errors, versions = await self.db.call(schema_step)
        if errors:
            return await self._fail(cr, "schema", {"errors": errors})
        await self._pass(cr, "schema", "checking", {"versions": len(versions)})

        # 2. policy check (OPA)
        deny = await self.policy.deny({
            "environment": env, "requested_by": cr["requested_by"],
            "versions": [{k: v[k] for k in ("type", "name", "content", "cold_paths")} for v in versions]})
        if deny:
            return await self._fail(cr, "policy", {"deny": deny, "engine": getattr(self.policy, "name", "policy")})
        await self._pass(cr, "policy", "checking", {"engine": getattr(self.policy, "name", "policy")})

        # 3. evaluation (C06)
        ev = await self.evals.evaluate(cr["version_ids"], env, cr_id)
        if not ev["passed"]:
            return await self._fail(cr, "evaluation", {"targets": [
                {k: t.get(k) for k in ("agentId", "businessUnit", "passed", "reasons")} for t in ev["targets"]]})
        await self._pass(cr, "evaluation", "checking", {"evaluator": ev.get("evaluator")})

        # 4. risk-based routing (C05-F2)
        required: dict[str, set[str]] = {}
        cold = False
        for v in versions:
            cold = cold or bool(v["cold_paths"])
            for role in v["approvers"]:
                required.setdefault(role, set()).update(v["scopes"])
        authors = sorted({v["author"] for v in versions})

        def route(tx: Tx):
            req = []
            for role, scopes in sorted(required.items()):
                eligible = [p for p in store.principals_for(tx, role, sorted(scopes))
                            if p != cr["requested_by"] and p not in authors]
                req.append({"role": role, "scopes": sorted(scopes), "eligible": eligible})
            guard_auto = set()
            for v in versions:
                agent_id = v["content"].get("agentId") or (v["content"].get("id") if v["type"] == "agent" else None)
                g = store.guardrail_for_agent(tx, agent_id)
                guard_auto |= set((g or {}).get("approvalPattern", {}).get("autoApproveRiskTiers", []))
            auto = (self.settings.auto_approval_enabled and not cold and cr["risk_tier"] in guard_auto
                    and cr["risk_tier"] in self.settings.auto_approve_risk_tiers)
            self._step(tx, cr_id, "routing", "info", "awaiting_approval",
                       {"required_roles": req, "cold": cold, "risk_tier": cr["risk_tier"], "auto_approve": auto,
                        "authors": authors})
            return auto

        auto = await self.db.call(route)
        cr = await self.db.call(self.get_cr, cr_id)
        await self._emit("config.check.completed", cr, step="routing", result="awaiting_approval")
        if auto:
            return await self._finalize(cr, "system:auto-approval")
        return cr

    async def _pass(self, cr, step, state, detail) -> None:
        await self.db.call(lambda tx: self._step(tx, cr["id"], step, "passed", state, detail))
        await self._emit("config.check.completed", cr, step=step, result="passed")

    async def _fail(self, cr, step, detail) -> dict[str, Any]:
        def run(tx: Tx):
            self._step(tx, cr["id"], step, "failed", "failed", detail)
            for vid in cr["version_ids"]:
                store.set_status(tx, vid, "rejected", "system:workflow", cr["id"])
            store.audit(tx, "system:workflow", f"change_request.{step}_failed", cr["id"], None, detail)

        await self.db.call(run)
        await self._emit("config.check.completed", cr, step=step, result="failed")
        return await self.db.call(self.get_cr, cr["id"])

    # ------------------------------------------------------------------ approvals (C05-F3)

    async def approve(self, cr_id: str, approver: str, role: str, decision: str, comment: str = "") -> dict[str, Any]:
        if decision not in ("approve", "reject"):
            raise Invalid("decision must be approve or reject")

        def run(tx: Tx) -> str:
            cr = self.get_cr(tx, cr_id)
            if cr["state"] not in ("awaiting_approval", "activated_pending_review"):
                raise Conflict(f"change request is {cr['state']}; nothing to approve")
            req = {r["role"]: r for r in cr["required_roles"]}
            if role not in req:
                raise Forbidden(f"role {role!r} is not required for this change; required: {sorted(req)}")
            authors = {store.get_version(tx, v)["author"] for v in cr["version_ids"]}
            if approver == cr["requested_by"] or approver in authors:
                raise Forbidden("self-approval is not allowed")
            if role not in store.roles_for(tx, approver, req[role]["scopes"]):
                raise Forbidden(f"{approver} is not a named {role} approver for {req[role]['scopes']}")
            if any(a["role"] == role and a["approver"] == approver for a in cr["approvals"]):
                raise Conflict("already recorded")
            tx.execute("INSERT INTO approval (id, change_request_id, approver, role, decision, comment, at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id("apv_"), cr_id, approver, role, decision, comment, now_iso()))
            store.audit(tx, approver, f"change_request.{decision}", cr_id, None, {"role": role, "comment": comment})
            if decision == "reject":
                self._step(tx, cr_id, "approval", "failed", "rejected", {"by": approver, "role": role})
                for v in cr["version_ids"]:
                    store.set_status(tx, v, "rejected", approver, cr_id)
                return "rejected"
            approved_roles = {a["role"] for a in cr["approvals"] if a["decision"] == "approve"} | {role}
            if set(req) <= approved_roles:
                return "complete_review" if cr["state"] == "activated_pending_review" else "complete"
            self._step(tx, cr_id, "approval", "pending", cr["state"],
                       {"by": approver, "role": role, "remaining": sorted(set(req) - approved_roles)})
            return "pending"

        outcome = await self.db.call(run)
        cr = await self.db.call(self.get_cr, cr_id)
        await self._emit("config.approval.recorded", cr, decision=decision, role=role, approver=approver)
        if outcome == "complete":
            return await self._finalize(cr, approver)
        if outcome == "complete_review":
            await self.db.call(lambda tx: self._step(tx, cr_id, "approval", "passed", "activated",
                                                     {"after_the_fact": True}))
            return await self.db.call(self.get_cr, cr_id)
        return cr

    async def _finalize(self, cr: dict[str, Any], actor: str) -> dict[str, Any]:
        if cr["cold"]:  # C05-F6: cold changes go to provisioning, never straight to activation
            await self.db.call(lambda tx: self._step(tx, cr["id"], "approval", "passed", "provisioning", {"by": actor}))
            cr = await self.db.call(self.get_cr, cr["id"])
            await self._emit("config.provisioning.requested", cr)
            if self.tasks is not None:
                await self.tasks.enqueue("/internal/v1/tasks/provision", {"change_request_id": cr["id"]},
                                         task_id=f"provision-{cr['id']}")
            elif self.provisioner is not None:
                return await self.provisioner.run(cr["id"])
            return cr
        await self.db.call(lambda tx: self._step(tx, cr["id"], "approval", "passed", "approved", {"by": actor}))
        return await self.activate(cr["id"], actor, cr["environment"])

    # ------------------------------------------------------------------ activation (C05-F5)

    def _activation_guard(self, tx: Tx, cr: dict[str, Any], environment: str) -> None:
        """No role can activate a version that failed a check (C05 acceptance)."""
        if any(s["result"] == "failed" for s in cr["steps"]):
            raise Forbidden("a check failed; this change can never be activated")
        for vid in cr["version_ids"]:
            r = tx.fetchone("SELECT passed FROM evaluation_run WHERE version_id = ? AND environment = ? "
                            "ORDER BY seq DESC LIMIT 1", (vid, environment))
            if not r or not r["passed"]:
                raise Forbidden(f"version {vid} has no passing evaluation in {environment}")

    def _insert_activation(self, tx: Tx, cr: dict[str, Any], environment: str, actor: str) -> list[tuple[dict, dict]]:
        out = []
        for vid in cr["version_ids"]:
            v = store.get_version(tx, vid)
            art = store.get_artifact(tx, v["artifact_id"])
            store.insert_pointer(tx, art["id"], environment, vid, actor, cr["id"])
            store.set_status(tx, vid, "approved", actor, cr["id"])
            store.audit(tx, actor, "version.activate", art["id"], vid, {"environment": environment, "cr": cr["id"]})
            out.append((art, v))
        return out

    async def activate(self, cr_id: str, actor: str, environment: str | None = None) -> dict[str, Any]:
        def run(tx: Tx):
            cr = self.get_cr(tx, cr_id)
            env = environment or cr["environment"]
            if cr["state"] != "approved":
                raise Forbidden(f"change request is {cr['state']}, not approved")
            self._activation_guard(tx, cr, env)
            pairs = self._insert_activation(tx, cr, env, actor)
            self._step(tx, cr_id, "activation", "passed", "activated", {"environment": env})
            return cr, env, pairs

        cr, env, pairs = await self.db.call(run)
        for art, v in pairs:  # C03-F7 independent audit copy
            await self.exporter.export(art, v, env, cr_id)
        await self._emit("config.version.activated", cr, environment=env)
        return await self.db.call(self.get_cr, cr_id)

    async def promote(self, cr_id: str, to_env: str, actor: str) -> dict[str, Any]:
        """dev -> test -> production (C05-F5): re-evaluate in the target environment, then move its pointers."""
        if to_env not in ENV_ORDER[1:]:
            raise Invalid("promote to test or production")
        prev = ENV_ORDER[ENV_ORDER.index(to_env) - 1]

        def check(tx: Tx):
            cr = self.get_cr(tx, cr_id)
            if cr["state"] != "activated":
                raise Conflict(f"change request is {cr['state']}")
            for vid in cr["version_ids"]:
                v = store.get_version(tx, vid)
                av = store.active_version(tx, v["artifact_id"], prev)
                if not av or av["id"] != vid:
                    raise Conflict(f"version {vid} is not active in {prev}")
            return cr

        cr = await self.db.call(check)
        ev = await self.evals.evaluate(cr["version_ids"], to_env, cr_id)
        if not ev["passed"]:
            await self.db.call(lambda tx: self._step(tx, cr_id, f"promotion:{to_env}", "failed", "activated",
                                                     {"targets": [t.get("reasons") for t in ev["targets"]]}))
            raise Forbidden(f"evaluation failed in {to_env}; not promoted")

        def run(tx: Tx):
            pairs = self._insert_activation(tx, cr, to_env, actor)
            self._step(tx, cr_id, f"promotion:{to_env}", "passed", "activated", {"by": actor})
            return pairs

        pairs = await self.db.call(run)
        for art, v in pairs:
            await self.exporter.export(art, v, to_env, cr_id)
        await self._emit("config.version.activated", cr, environment=to_env)
        return await self.db.call(self.get_cr, cr_id)

    # ------------------------------------------------------------------ rollback (C02-F6, C05-F7)

    async def rollback(self, artifact_id: str, environment: str, actor: str, version_id: str | None = None,
                       emergency: bool = False, reason: str = "rollback") -> dict[str, Any]:
        def run(tx: Tx):
            hist = store.pointer_history(tx, artifact_id, environment)
            if not hist:
                raise Conflict(f"nothing is active in {environment}")
            current = hist[0]["version_id"]
            known_good = [h["version_id"] for h in hist if h["version_id"] != current]
            target = version_id or (known_good[0] if known_good else None)
            if target is None or target not in known_good:
                raise Forbidden("rollback target must be a version that was active here before (last known good)")
            v = store.get_version(tx, target)
            art = store.get_artifact(tx, artifact_id)
            cls = self.mm.classify(art["type"], store.get_version(tx, current)["content"], v["content"])
            cr_id = new_id("cr_")
            tx.execute("INSERT INTO change_request (id, version_ids, reason, risk_tier, environment, requested_by, "
                       "created_at, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted')",
                       (cr_id, dumps([target]), reason, self._risk_tier(tx, v), environment, actor, now_iso()))
            scopes = store.scopes_of(art, v["content"])
            req = [{"role": r, "scopes": scopes, "eligible": [p for p in store.principals_for(tx, r, scopes) if p != actor]}
                   for r in sorted(cls.approvers)]
            if emergency:
                store.insert_pointer(tx, artifact_id, environment, target, actor, cr_id)
                store.audit(tx, actor, "version.emergency_rollback", artifact_id, target, {"environment": environment})
                self._step(tx, cr_id, "routing", "info", "activated_pending_review",
                           {"required_roles": req, "cold": False, "emergency": True})
            else:
                # the target passed its checks when it was active; re-use that evaluation
                self._step(tx, cr_id, "routing", "info", "awaiting_approval",
                           {"required_roles": req, "cold": False, "rollback_of": current})
            return cr_id, art, v

        cr_id, art, v = await self.db.call(run)
        cr = await self.db.call(self.get_cr, cr_id)
        if emergency:
            await self.exporter.export(art, v, environment, cr_id)
            await self._emit("config.version.activated", cr, environment=environment, emergency=True)
        return cr

    # ------------------------------------------------------------------ bootstrap (seed only)

    async def bootstrap(self, version_ids: list[str], environment: str) -> dict[str, Any]:
        """Load the very first configuration into an empty registry: all checks still run
        (schema, policy, evaluation); the platform's own system principal approves."""
        if not self.settings.allow_bootstrap:
            raise Forbidden("bootstrap disabled")
        if await self.db.call(lambda tx: tx.fetchone("SELECT 1 AS x FROM active_pointer LIMIT 1")):
            raise Conflict("registry already has active configuration; use change requests")
        cr = await self.submit(version_ids, "bootstrap: seed pilot data set", environment, "system:seed")
        if cr["state"] != "awaiting_approval":
            return cr

        def approve_all(tx: Tx):
            for r in cr["required_roles"]:
                tx.execute("INSERT INTO approval (id, change_request_id, approver, role, decision, comment, at) "
                           "VALUES (?, ?, ?, ?, 'approve', 'bootstrap', ?)",
                           (new_id("apv_"), cr["id"], "system:bootstrap", r["role"], now_iso()))

        await self.db.call(approve_all)
        return await self._finalize(await self.db.call(self.get_cr, cr["id"]), "system:bootstrap")
