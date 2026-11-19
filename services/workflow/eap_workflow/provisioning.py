"""C11 Provisioning pipeline: an approved cold change -> provisioned platform resources.

Started by the promotion workflow with no manual hand-off (C11-F1), as a Cloud Tasks job on GCP.
For dev, test, then production (up to the change request's environment) it compiles the spec,
applies it through the adapter, records a deployment and runs the evaluations (C11-F2). Hot
configuration in the change is activated in an environment only after provisioning there
succeeded (C11-F3). Any failure rolls the platform back to the previous deployment (C11-F4).
"""

from __future__ import annotations

import logging
from typing import Any

from eap_compiler import check_supported, compile_spec
from eap_compiler.adapter import PlatformState, diff_plan
from eap_core.db import Tx
from eap_store import store

from .promotion import ENV_ORDER, PromotionEngine

log = logging.getLogger("eap.provisioning")


class Provisioner:
    def __init__(self, engine: PromotionEngine, adapter: Any):
        self.engine = engine
        self.adapter = adapter
        self.db = engine.db
        engine.provisioner = self

    def _agents(self, tx: Tx, cr: dict[str, Any]) -> list[str]:
        agents = set()
        for vid in cr["version_ids"]:
            v = store.get_version(tx, vid)
            art = store.get_artifact(tx, v["artifact_id"])
            if art["type"] == "agent":
                agents.add(v["content"]["id"])
            elif v["content"].get("agentId"):
                agents.add(v["content"]["agentId"])
        if not agents:  # a shared cold artifact (tool_def, knowledge_source): every agent that binds it
            for vid in cr["version_ids"]:
                v = store.get_version(tx, vid)
                for d in store.dependents(tx, self.engine.mm, v["artifact_id"]):
                    lv = store.latest_version(tx, d["id"])
                    if lv and lv["content"].get("agentId"):
                        agents.add(lv["content"]["agentId"])
        return sorted(agents)

    async def run(self, cr_id: str) -> dict[str, Any]:
        eng = self.engine
        cr = await self.db.call(eng.get_cr, cr_id)
        if cr["state"] != "provisioning":
            return cr
        envs = ENV_ORDER[: ENV_ORDER.index(cr["environment"]) + 1]
        overlay_ids = cr["version_ids"]
        for env in envs:
            def prep(tx: Tx, env=env):
                overlay = {store.get_version(tx, v)["artifact_id"]: v for v in overlay_ids}
                snap = store.snapshot(tx, env, overlay)
                return [(a, compile_spec(snap, a)) for a in self._agents(tx, cr)]

            specs = await self.db.call(prep)
            applied: list[tuple[str, PlatformState]] = []
            failure = None
            for agent_id, spec in specs:
                try:
                    check_supported(self.adapter, spec)  # C10-F7
                    before = await self.adapter.read_state(agent_id, env)
                    plan = self.adapter.plan(spec, before, env)
                    result = await self.adapter.apply(plan)
                except Exception as e:  # adapter or credential error
                    failure = (agent_id, str(e))
                    break
                applied.append((agent_id, before))
                await self._record(cr, spec, env, result.status, {"applied": result.applied, "error": result.error,
                                                                  "fingerprint": result.state_fingerprint})
                if result.status != "succeeded":
                    failure = (agent_id, result.error or "apply failed")
                    break
            if failure is None:
                ev = await eng.evals.evaluate(cr["version_ids"], env, cr_id)  # C11-F2
                if not ev["passed"]:
                    failure = ("evaluation", "; ".join(r for t in ev["targets"] for r in t.get("reasons", [])))
            if failure is not None:
                await self._rollback(cr, env, applied, specs)
                await self.db.call(lambda tx, env=env: eng._step(tx, cr_id, "provisioning", "failed", "failed",
                                                                 {"environment": env, "agent": failure[0], "error": failure[1]}))
                await eng._emit("config.provisioning.failed", cr, environment=env, error=failure[1])
                return await self.db.call(eng.get_cr, cr_id)

            def activate(tx: Tx, env=env):  # C11-F3
                c = eng.get_cr(tx, cr_id)
                eng._activation_guard(tx, c, env)
                pairs = eng._insert_activation(tx, c, env, "system:provisioning")
                eng._step(tx, cr_id, "provisioning", "passed", "provisioning" if env != envs[-1] else "activated",
                          {"environment": env, "agents": [a for a, _ in specs]})
                return pairs

            pairs = await self.db.call(activate)
            for art, v in pairs:
                await eng.exporter.export(art, v, env, cr_id)
            await eng._emit("config.version.activated", cr, environment=env)
        await eng._emit("config.provisioning.completed", cr)
        return await self.db.call(eng.get_cr, cr_id)

    async def _record(self, cr: dict[str, Any], spec: dict[str, Any], env: str, status: str, detail: dict[str, Any],
                      change_request_id: str | None = None) -> None:
        def run(tx: Tx):
            for ch in spec.get("channels") or ["api"]:
                for bu, p in (spec.get("profiles") or {None: {"versionId": None}}).items():
                    store.record_deployment(tx, spec["agentVersionId"], p["versionId"], ch, self.adapter.id, env,
                                            self.adapter.version, status, {**detail, "businessUnit": bu},
                                            cr["id"] if cr else change_request_id)

        await self.db.call(run)

    async def _rollback(self, cr: dict[str, Any], env: str, applied: list[tuple[str, PlatformState]],
                        specs: list[tuple[str, dict[str, Any]]]) -> None:
        """C11-F4: put every touched agent back to the state of its previous deployment."""
        spec_by_agent = dict(specs)
        for agent_id, before in applied:
            live = await self.adapter.read_state(agent_id, env)
            plan = diff_plan(agent_id, env, before.resources, live)
            if plan.empty:
                continue
            result = await self.adapter.apply(plan)
            await self._record(cr, spec_by_agent[agent_id], env, "rolled_back",
                               {"applied": result.applied, "restored_fingerprint": before.fingerprint})

    async def reconcile(self, agent_id: str, environment: str, actor: str = "system:reconcile") -> dict[str, Any]:
        """Re-create or repair an agent on the platform from the registry alone (C10 acceptance, test 9)."""
        snap = await self.db.call(lambda tx: store.snapshot(tx, environment))
        spec = compile_spec(snap, agent_id)
        check_supported(self.adapter, spec)
        live = await self.adapter.read_state(agent_id, environment)
        plan = self.adapter.plan(spec, live, environment)
        result = await self.adapter.apply(plan)
        await self._record(None, spec, environment, result.status,
                           {"applied": result.applied, "reconcile": True, "by": actor})
        return {"status": result.status, "applied": result.applied, "fingerprint": result.state_fingerprint}
