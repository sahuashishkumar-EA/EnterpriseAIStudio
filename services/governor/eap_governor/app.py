"""Governor: control plane API + web console.

Public API (console + automation), prefix /api/v1 - authenticated users with per-BU roles.
Internal API, prefix /internal/v1 - service callers only (the runtime; Cloud Tasks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from eap_core import plugins, schema
from eap_core.auth import Authenticator, AuthError, Principal
from eap_core.db import Database, Tx, dumps, expand_url, new_id, now_iso
from eap_core.events import Event, Publisher, publisher_from_env
from eap_core.policy import dump_policy
from eap_core.tasks import LocalTaskQueue, TaskQueue, queue_from_env

from . import evals, release, security, seed, store
from .config import Settings
from .runtime_client import RuntimeClient
from .security import Forbidden, RoleSet
from .store import Conflict, Invalid, NotFound

log = logging.getLogger("eap.governor")
HERE = Path(__file__).parent


@dataclass
class Gov:
    settings: Settings
    db: Database
    auth: Authenticator
    publisher: Publisher
    runtime: RuntimeClient
    tasks: TaskQueue

    async def emit(self, event_type: str, **data: Any) -> None:
        try:
            await self.publisher.publish(Event(event_type=event_type, data=data))
        except Exception:
            log.exception("publishing %s failed", event_type)


@dataclass
class Caller:
    principal: Principal
    roles: RoleSet

    @property
    def id(self) -> str:
        return self.principal.id


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers)


def _error(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return _json(body, status)


Handler = Callable[[Request, Caller], Awaitable[Response]]


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    publisher: Publisher | None = None,
    runtime_transport: httpx.AsyncBaseTransport | None = None,
    tasks: TaskQueue | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    db = db or Database(expand_url(settings.database_url))
    db.migrate(HERE / "migrations")
    with db.transaction() as tx:
        if settings.seed_catalog:
            seed.seed(tx)
        for admin in settings.bootstrap_admins:
            security.grant(tx, admin, "*", "platform_admin", "system:bootstrap")
    gov = Gov(
        settings=settings,
        db=db,
        auth=Authenticator(settings.auth),
        publisher=publisher or publisher_from_env(),
        runtime=RuntimeClient(settings.runtime_url, settings.runtime_auth, transport=runtime_transport),
        tasks=tasks or queue_from_env(),
    )

    # ----------------------------------------------------------------- plumbing

    def api(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            try:
                p = await gov.auth.authenticate(request.headers)
                roles = await db.call(security.load_roles, p)
                return await fn(request, Caller(p, roles))
            except AuthError as e:
                return _error(e.status, "unauthenticated", str(e))
            except Forbidden as e:
                return _error(403, "forbidden", str(e))
            except NotFound as e:
                return _error(404, "not_found", str(e))
            except Conflict as e:
                return _error(409, "conflict", str(e), e.details)
            except Invalid as e:
                return _error(422, "invalid", str(e), e.details)
            except evals.DatasetError as e:
                return _error(422, "invalid_dataset", str(e))
            except ValueError as e:
                return _error(400, "bad_request", str(e))

        return wrapper

    def internal(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def check(request: Request, c: Caller) -> Response:
            p = c.principal
            name = gov.auth.service_name(p) or (p.sub if p.mode == "dev" else None)
            if p.kind != "service" or name not in settings.internal_callers + [settings.service_name]:
                raise Forbidden("internal endpoint: service callers only")
            return await fn(request, c)

        return api(check)

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception as e:
            raise ValueError("request body must be JSON") from e
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    async def agent_bu(name: str) -> str:
        return (await db.call(store.get_agent, name))["owner_bu"]

    # -------------------------------------------------------------- identity etc.

    async def me(request: Request, c: Caller) -> Response:
        return _json({"principal": c.id, "kind": c.principal.kind, "roles": c.roles.to_dict()})

    async def list_plugins(request: Request, c: Caller) -> Response:
        return _json(plugins.catalog_json())

    async def definition_schema(request: Request, c: Caller) -> Response:
        return _json(schema.AgentDefinition.model_json_schema())

    # -------------------------------------------------------------------- agents

    async def agents_list(request: Request, c: Caller) -> Response:
        return _json({"agents": await db.call(store.list_agents, c.roles.visible_bus())})

    async def agents_create(request: Request, c: Caller) -> Response:
        b = await body(request)
        bu = b.get("owner_bu") or ""
        c.roles.require(bu, "author")
        await db.call(store.create_agent, b.get("name", ""), bu, b.get("description", ""), c.id, b.get("definition"))
        return _json(await db.call(lambda tx: _agent_detail(tx, b["name"])), 201)

    def _agent_detail(tx: Tx, name: str) -> dict[str, Any]:
        a = store.get_agent(tx, name)
        return {
            **a,
            "draft": store.get_draft(tx, name),
            "versions": store.list_versions(tx, name),
            "aliases": store.list_aliases(tx, name),
        }

    async def agent_get(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        return _json(await db.call(_agent_detail, name))

    async def draft_put(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "author")
        b = await body(request)
        d = await db.call(store.save_draft, name, b.get("definition", b), c.id)
        return _json(d)

    async def draft_validate(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        bu = await agent_bu(name)
        c.roles.require(bu, "viewer")
        raw = await request.body()
        if raw.strip():
            data = await body(request)
            defn = store.parse_or_invalid(data.get("definition", data))
        else:
            defn = schema.parse_definition((await db.call(store.get_draft, name))["definition"])
        return _json(await db.call(release.validate, defn, bu))

    async def draft_diff(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")

        def run(tx: Tx) -> dict[str, Any]:
            draft = schema.parse_definition(store.get_draft(tx, name)["definition"])
            latest = store.latest_version(tx, name)
            prev = schema.parse_definition(store.get_version(tx, name, latest)["definition"]) if latest else None
            return {
                "latest_version": latest,
                "breaking_changes": schema.breaking_changes(prev, draft) if prev else [],
                "required_bump": schema.required_bump(prev, draft),
                "previous": prev.canonical() if prev else None,
                "draft": draft.canonical(),
            }

        return _json(await db.call(run))

    async def versions_list(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        return _json({"versions": await db.call(store.list_versions, name)})

    async def version_get(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        return _json(await db.call(store.get_version, name, request.path_params["version"]))

    # --------------------------------------------------------------------- evals

    async def suites_list(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")

        def run(tx: Tx) -> list[dict[str, Any]]:
            rows = tx.fetchall("SELECT * FROM eval_suites WHERE agent_name = ? ORDER BY name", (name,))
            from eap_core.db import loads

            for r in rows:
                r["cases"] = loads(r["cases"], [])
                r["assertions"] = loads(r["assertions"], [])
                r["required"] = bool(r["required"])
            return rows

        return _json({"suites": await db.call(run)})

    async def suites_put(request: Request, c: Caller) -> Response:
        """Create/replace a suite. Body: {name, cases | content+format, assertions, required}."""
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "author")
        b = await body(request)
        suite_name = (b.get("name") or "").strip()
        if not suite_name:
            raise Invalid("suite name required")
        if "cases" in b:
            cases = evals.parse_dataset(dumps(b["cases"]), "json")
            source = "json"
        elif "content" in b:
            cases = evals.parse_dataset(b["content"], b.get("format"), b.get("filename"))
            source = (b.get("format") or (b.get("filename") or "").rsplit(".", 1)[-1] or "auto").lower()
        else:
            raise Invalid("provide cases (list) or content (+format)")
        assertions = b.get("assertions", [])
        for a in assertions:
            evals.check_assertion_spec(a)
        required = 1 if b.get("required", True) else 0

        def run(tx: Tx) -> dict[str, Any]:
            store.get_agent(tx, name)
            cur = tx.fetchone("SELECT id FROM eval_suites WHERE agent_name = ? AND name = ?", (name, suite_name))
            sid = cur["id"] if cur else new_id("suite_")
            if cur:
                tx.execute(
                    "UPDATE eval_suites SET cases = ?, assertions = ?, required = ?, source = ? WHERE id = ?",
                    (dumps(cases), dumps(assertions), required, source, sid),
                )
            else:
                tx.execute(
                    "INSERT INTO eval_suites (id, agent_name, name, cases, assertions, required, source, created_by, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, name, suite_name, dumps(cases), dumps(assertions), required, source, c.id, now_iso()),
                )
            store.audit(tx, c.id, "eval.suite.save", f"agent/{name}", {"suite": suite_name, "cases": len(cases)})
            return {"id": sid, "name": suite_name, "cases": len(cases), "required": bool(required)}

        return _json(await db.call(run), 201)

    async def suite_delete(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "author")
        sid = request.path_params["suite_id"]

        def run(tx: Tx) -> None:
            tx.execute("DELETE FROM eval_runs WHERE suite_id = ? AND agent_name = ?", (sid, name))
            if not tx.execute("DELETE FROM eval_suites WHERE id = ? AND agent_name = ?", (sid, name)):
                raise NotFound("suite not found")
            store.audit(tx, c.id, "eval.suite.delete", f"agent/{name}", {"suite_id": sid})

        await db.call(run)
        return _json({"deleted": sid})

    async def evals_run(request: Request, c: Caller) -> Response:
        """Body: {suite_ids?: [...], target: 'draft'|'version', version?: '1.2.0'}; ?wait=true runs inline."""
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "author")
        b = await body(request) if (await request.body()).strip() else {}
        target = b.get("target", "draft")
        version = b.get("version")
        if target not in ("draft", "version") or (target == "version" and not version):
            raise Invalid("target must be 'draft' or 'version' (with version)")

        def create(tx: Tx) -> list[str]:
            if target == "draft":
                h = store.get_draft(tx, name)["content_hash"]
            else:
                h = store.get_version(tx, name, version)["content_hash"]
            q = "SELECT id FROM eval_suites WHERE agent_name = ?"
            suites = [r["id"] for r in tx.fetchall(q, (name,))]
            if b.get("suite_ids"):
                suites = [s for s in suites if s in set(b["suite_ids"])]
            if not suites:
                raise Invalid("no eval suites to run")
            ids = []
            for sid in suites:
                rid = new_id("evr_")
                tx.execute(
                    "INSERT INTO eval_runs (id, agent_name, suite_id, target, target_version, content_hash, status, "
                    "started_by, started_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (rid, name, sid, target, version, h, c.id, now_iso()),
                )
                ids.append(rid)
            store.audit(tx, c.id, "eval.run", f"agent/{name}", {"runs": ids, "target": target, "content_hash": h})
            return ids

        ids = await db.call(create)
        if request.query_params.get("wait", "false").lower() == "true":
            for rid in ids:
                await execute_eval_run(rid)
            runs = [await db.call(release.load_eval_run, rid) for rid in ids]
            return _json({"runs": runs})
        for rid in ids:
            await gov.tasks.enqueue("/internal/v1/tasks/eval-run", {"run_id": rid}, task_id=rid)
        return _json({"runs": [{"id": rid, "status": "queued"} for rid in ids]}, 202)

    async def execute_eval_run(run_id: str) -> None:
        def prepare(tx: Tx) -> dict[str, Any] | None:
            run = release.load_eval_run(tx, run_id)
            if run["status"] not in ("queued", "error"):
                return None
            from eap_core.db import loads

            suite = tx.fetchone("SELECT * FROM eval_suites WHERE id = ?", (run["suite_id"],))
            agent = store.get_agent(tx, run["agent_name"])
            if run["target"] == "draft":
                d = store.get_draft(tx, run["agent_name"])
                if d["content_hash"] != run["content_hash"]:
                    raise Conflict("draft changed since the eval was queued")
                defn, ver = schema.parse_definition(d["definition"]), "draft"
            else:
                v = store.get_version(tx, run["agent_name"], run["target_version"])
                defn, ver = schema.parse_definition(v["definition"]), run["target_version"]
            policy = store.effective_policy(tx, agent["owner_bu"])
            tx.execute("UPDATE eval_runs SET status = 'running' WHERE id = ?", (run_id,))
            return {
                "release": store.release_payload(tx, defn, policy, version=ver, status="draft"),
                "policy": dump_policy(policy),
                "cases": loads(suite["cases"], []),
                "assertions": loads(suite["assertions"], []),
                "principal": run["started_by"],
                "models": {m["id"]: m for m in store.list_catalog(tx, "models") if m["status"] == "active"},
            }

        try:
            prep = await db.call(prepare)
        except Exception as e:
            await db.call(release.record_eval_run, {"id": run_id, "status": "error", "passed": 0, "total": 0,
                                                    "pass_rate": 0.0, "cost_usd": 0.0, "results": [], "error": str(e)})
            return
        if prep is None:
            return
        try:
            out = await evals.run_cases(
                gov.runtime,
                prep["release"],
                prep["policy"],
                prep["cases"],
                prep["assertions"],
                principal=prep["principal"],
                judge_model=settings.judge_model,
                models_catalog=prep["models"],
            )
            await db.call(release.record_eval_run, {"id": run_id, "status": "completed", **out})
        except Exception as e:
            log.exception("eval run %s failed", run_id)
            await db.call(release.record_eval_run, {"id": run_id, "status": "error", "passed": 0, "total": 0,
                                                    "pass_rate": 0.0, "cost_usd": 0.0, "results": [], "error": str(e)})

    if isinstance(gov.tasks, LocalTaskQueue):
        gov.tasks.register("/internal/v1/tasks/eval-run", lambda p: execute_eval_run(p["run_id"]))

    async def task_eval_run(request: Request, c: Caller) -> Response:
        b = await body(request)
        await execute_eval_run(b["run_id"])
        return _json({"ok": True})

    async def eval_runs_list(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        rows = await db.call(
            lambda tx: tx.fetchall(
                "SELECT r.id, r.suite_id, s.name AS suite, r.target, r.target_version, r.content_hash, r.status, "
                "r.passed, r.total, r.pass_rate, r.cost_usd, r.error, r.started_by, r.started_at, r.finished_at "
                "FROM eval_runs r JOIN eval_suites s ON s.id = r.suite_id WHERE r.agent_name = ? "
                "ORDER BY r.started_at DESC LIMIT 100",
                (name,),
            )
        )
        return _json({"runs": rows})

    async def eval_run_get(request: Request, c: Caller) -> Response:
        run = await db.call(release.load_eval_run, request.path_params["run_id"])
        c.roles.require(await agent_bu(run["agent_name"]), "viewer")
        return _json(run)

    # --------------------------------------------------------- approvals/publish

    async def approvals_post(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "approver")
        b = await body(request)
        return _json(
            await db.call(release.approve, name, c.id, b.get("decision", "approve"), b.get("comment", ""),
                          b.get("content_hash")),
            201,
        )

    async def approvals_list(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        rows = await db.call(
            lambda tx: tx.fetchall("SELECT * FROM approvals WHERE agent_name = ? ORDER BY at DESC", (name,))
        )
        return _json({"approvals": rows})

    async def release_check(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        return _json(await db.call(release.release_check, name, c.id, request.query_params.get("bump")))

    async def publish(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        bu = await agent_bu(name)
        c.roles.require(bu, "publisher")
        b = await body(request) if (await request.body()).strip() else {}

        def run(tx: Tx) -> dict[str, Any]:
            res = release.publish(tx, name, c.id, b.get("bump"), b.get("changelog", ""))
            if b.get("alias"):
                res["alias"] = store.move_alias(tx, name, b["alias"], res["version"], c.id, reason="publish")
            return res

        res = await db.call(run)
        await gov.emit("version.published", agent=name, version=res["version"], content_hash=res["content_hash"])
        if res.get("alias"):
            a = res["alias"]
            await gov.emit("alias.moved", agent=name, alias=a["alias"], version=a["version"],
                           canary_version=a["canary_version"], canary_weight=a["canary_weight"])
        return _json({k: v for k, v in res.items() if k != "check"} | {"breaking_changes": res["check"]["breaking_changes"]}, 201)

    # -------------------------------------------------------------------- aliases

    async def aliases_list(request: Request, c: Caller) -> Response:
        name = request.path_params["name"]
        c.roles.require(await agent_bu(name), "viewer")
        return _json({"aliases": await db.call(store.list_aliases, name)})

    async def alias_put(request: Request, c: Caller) -> Response:
        name, alias = request.path_params["name"], request.path_params["alias"]
        c.roles.require(await agent_bu(name), "publisher")
        b = await body(request)
        if not b.get("version"):
            raise Invalid("version required")
        a = await db.call(
            store.move_alias, name, alias, b["version"], c.id,
            canary_version=b.get("canary_version"), canary_weight=int(b.get("canary_weight", 0) or 0),
            reason=b.get("reason", ""), allow_flagged=bool(b.get("allow_flagged", False)),
        )
        await gov.emit("alias.moved", agent=name, alias=alias, version=a["version"],
                       canary_version=a["canary_version"], canary_weight=a["canary_weight"])
        return _json(a)

    async def alias_rollback(request: Request, c: Caller) -> Response:
        name, alias = request.path_params["name"], request.path_params["alias"]
        c.roles.require(await agent_bu(name), "publisher")
        b = await body(request) if (await request.body()).strip() else {}
        a = await db.call(store.rollback_alias, name, alias, c.id, b.get("reason", "rollback"))
        await gov.emit("alias.moved", agent=name, alias=alias, version=a["version"], canary_version=None,
                       canary_weight=0, rollback=True)
        return _json(a)

    async def alias_history(request: Request, c: Caller) -> Response:
        name, alias = request.path_params["name"], request.path_params["alias"]
        c.roles.require(await agent_bu(name), "viewer")
        return _json({"history": await db.call(store.alias_history, name, alias)})

    async def graph(request: Request, c: Caller) -> Response:
        return _json(await db.call(store.graph, request.query_params.get("agent")))

    # -------------------------------------------------------------------- catalog

    async def catalog_list(request: Request, c: Caller) -> Response:
        kind = request.path_params["kind"]
        if kind not in store.CATALOG_TABLES:
            raise NotFound("unknown catalog kind")
        return _json({kind: await db.call(store.list_catalog, kind)})

    async def catalog_put(request: Request, c: Caller) -> Response:
        kind, id_ = request.path_params["kind"], request.path_params["id"]
        if kind not in store.CATALOG_TABLES:
            raise NotFound("unknown catalog kind")
        b = await body(request)
        if kind == "models" or not b.get("owner_bu"):
            if not c.roles.is_platform_admin():
                raise Forbidden("platform_admin required")
        else:
            c.roles.require(b["owner_bu"], "bu_admin")
        if kind == "models" and "status" in b:
            # status changes go through the status endpoint so dependents are flagged
            existing = await db.call(lambda tx: tx.fetchone("SELECT status FROM catalog_models WHERE id = ?", (id_,)))
            if existing and existing["status"] != b["status"]:
                raise Invalid("use PATCH /api/v1/catalog/models/{id}/status to change status")
        entry = await db.call(store.upsert_catalog, kind, id_, b, c.id)
        await gov.emit("catalog.updated", kind=kind, id=id_)
        return _json(entry)

    async def model_status(request: Request, c: Caller) -> Response:
        if not c.roles.is_platform_admin():
            raise Forbidden("platform_admin required")
        id_ = request.path_params["id"]
        b = await body(request)
        flagged = await db.call(store.set_model_status, id_, b.get("status", ""), c.id, b.get("reason", ""))
        await gov.emit("model.status_changed", model=id_, status=b["status"],
                       flagged_versions=[f"{a}@{v}" for a, v in flagged])
        for a, v in flagged:
            await gov.emit("version.flagged", agent=a, version=v, reason=f"model {id_} is {b['status']}")
        return _json({"model": id_, "status": b["status"], "flagged_versions": [{"agent": a, "version": v} for a, v in flagged]})

    # ------------------------------------------------------------------- policies

    async def policy_platform_get(request: Request, c: Caller) -> Response:
        return _json(dump_policy(await db.call(store.get_platform_policy)))

    async def policy_platform_put(request: Request, c: Caller) -> Response:
        if not c.roles.is_platform_admin():
            raise Forbidden("platform_admin required")
        p = await db.call(store.put_platform_policy, await body(request), c.id)
        await gov.emit("policy.updated", scope="platform")
        return _json(dump_policy(p))

    async def policy_bu_get(request: Request, c: Caller) -> Response:
        bu = request.path_params["bu"]
        c.roles.require(bu, "viewer")
        o = await db.call(store.get_bu_overlay, bu)
        return _json(dump_policy(o) if o else {})

    async def policy_bu_put(request: Request, c: Caller) -> Response:
        bu = request.path_params["bu"]
        c.roles.require(bu, "bu_admin")
        o = await db.call(store.put_bu_overlay, bu, await body(request), c.id)
        await gov.emit("policy.updated", scope=f"bu:{bu}")
        return _json(dump_policy(o))

    async def policy_effective(request: Request, c: Caller) -> Response:
        bu = request.path_params["bu"]
        c.roles.require(bu, "viewer")
        return _json(dump_policy(await db.call(store.effective_policy, bu)))

    # -------------------------------------------------------------- role bindings

    async def roles_list(request: Request, c: Caller) -> Response:
        bus = c.roles.visible_bus()
        rows = await db.call(lambda tx: tx.fetchall("SELECT * FROM role_bindings ORDER BY bu, principal, role"))
        if bus is not None:
            rows = [r for r in rows if r["bu"] in bus]
        return _json({"bindings": rows})

    async def roles_post(request: Request, c: Caller) -> Response:
        b = await body(request)
        bu, role = b.get("bu", ""), b.get("role", "")
        if bu == "*" or role in ("platform_admin", "bu_admin"):
            if not c.roles.is_platform_admin() and not (role == "bu_admin" and c.roles.has(bu, "bu_admin")):
                raise Forbidden("platform_admin required")
        else:
            c.roles.require(bu, "bu_admin")

        def run(tx: Tx) -> None:
            security.grant(tx, b["principal"], bu, role, c.id)
            store.audit(tx, c.id, "role.grant", f"roles/{bu}", {"principal": b["principal"], "role": role})

        await db.call(run)
        return _json({"granted": b}, 201)

    async def roles_delete(request: Request, c: Caller) -> Response:
        b = await body(request)
        bu = b.get("bu", "")
        if bu == "*":
            if not c.roles.is_platform_admin():
                raise Forbidden("platform_admin required")
        else:
            c.roles.require(bu, "bu_admin")

        def run(tx: Tx) -> int:
            n = security.revoke(tx, b["principal"], bu, b["role"])
            store.audit(tx, c.id, "role.revoke", f"roles/{bu}", {"principal": b["principal"], "role": b["role"]})
            return n

        return _json({"revoked": await db.call(run)})

    async def audit(request: Request, c: Caller) -> Response:
        target = request.query_params.get("target")
        if target and target.startswith("agent/"):
            c.roles.require(await agent_bu(target.split("/")[1]), "viewer")
        elif not c.roles.is_platform_admin():
            raise Forbidden("platform_admin required for the global audit log; filter by target=agent/<name>")
        return _json({"events": await db.call(store.list_audit, target, int(request.query_params.get("limit", 200)))})

    # ------------------------------------------------------------------- internal

    async def resolve(request: Request, c: Caller) -> Response:
        bundle = await db.call(store.resolve, request.path_params["name"], request.path_params["alias"])
        etag = bundle["etag"]
        headers = {"ETag": etag, "Cache-Control": "private, max-age=30"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return _json(bundle, headers=headers)

    async def internal_agents(request: Request, c: Caller) -> Response:
        alias = request.query_params.get("alias", "prod")
        return _json({"agents": await db.call(store.mcp_exposed, alias)})

    async def healthz(request: Request) -> Response:
        try:
            db.fetchone("SELECT 1 AS ok")
            return _json({"ok": True, "service": "governor"})
        except Exception as e:  # pragma: no cover
            return _json({"ok": False, "error": str(e)}, 503)

    async def index(request: Request) -> Response:
        return FileResponse(HERE / "static" / "index.html")

    A = "/api/v1"
    routes = [
        Route("/healthz", healthz),
        Route("/", index),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        Route(f"{A}/me", api(me)),
        Route(f"{A}/plugins", api(list_plugins)),
        Route(f"{A}/schema/definition", api(definition_schema)),
        Route(f"{A}/agents", api(agents_list), methods=["GET"]),
        Route(f"{A}/agents", api(agents_create), methods=["POST"]),
        Route(f"{A}/agents/{{name}}", api(agent_get)),
        Route(f"{A}/agents/{{name}}/draft", api(draft_put), methods=["PUT"]),
        Route(f"{A}/agents/{{name}}/draft/validate", api(draft_validate), methods=["POST"]),
        Route(f"{A}/agents/{{name}}/draft/diff", api(draft_diff)),
        Route(f"{A}/agents/{{name}}/versions", api(versions_list)),
        Route(f"{A}/agents/{{name}}/versions/{{version}}", api(version_get)),
        Route(f"{A}/agents/{{name}}/evals/suites", api(suites_list), methods=["GET"]),
        Route(f"{A}/agents/{{name}}/evals/suites", api(suites_put), methods=["POST", "PUT"]),
        Route(f"{A}/agents/{{name}}/evals/suites/{{suite_id}}", api(suite_delete), methods=["DELETE"]),
        Route(f"{A}/agents/{{name}}/evals/run", api(evals_run), methods=["POST"]),
        Route(f"{A}/agents/{{name}}/evals/runs", api(eval_runs_list)),
        Route(f"{A}/evals/runs/{{run_id}}", api(eval_run_get)),
        Route(f"{A}/agents/{{name}}/approvals", api(approvals_list), methods=["GET"]),
        Route(f"{A}/agents/{{name}}/approvals", api(approvals_post), methods=["POST"]),
        Route(f"{A}/agents/{{name}}/release-check", api(release_check)),
        Route(f"{A}/agents/{{name}}/publish", api(publish), methods=["POST"]),
        Route(f"{A}/agents/{{name}}/aliases", api(aliases_list)),
        Route(f"{A}/agents/{{name}}/aliases/{{alias}}", api(alias_put), methods=["PUT"]),
        Route(f"{A}/agents/{{name}}/aliases/{{alias}}/rollback", api(alias_rollback), methods=["POST"]),
        Route(f"{A}/agents/{{name}}/aliases/{{alias}}/history", api(alias_history)),
        Route(f"{A}/graph", api(graph)),
        Route(f"{A}/catalog/models/{{id}}/status", api(model_status), methods=["PATCH", "POST"]),
        Route(f"{A}/catalog/{{kind}}", api(catalog_list)),
        Route(f"{A}/catalog/{{kind}}/{{id}}", api(catalog_put), methods=["PUT"]),
        Route(f"{A}/policies/platform", api(policy_platform_get), methods=["GET"]),
        Route(f"{A}/policies/platform", api(policy_platform_put), methods=["PUT"]),
        Route(f"{A}/policies/bu/{{bu}}", api(policy_bu_get), methods=["GET"]),
        Route(f"{A}/policies/bu/{{bu}}", api(policy_bu_put), methods=["PUT"]),
        Route(f"{A}/policies/effective/{{bu}}", api(policy_effective)),
        Route(f"{A}/role-bindings", api(roles_list), methods=["GET"]),
        Route(f"{A}/role-bindings", api(roles_post), methods=["POST"]),
        Route(f"{A}/role-bindings", api(roles_delete), methods=["DELETE"]),
        Route(f"{A}/audit", api(audit)),
        Route("/internal/v1/resolve/{name}/{alias}", internal(resolve)),
        Route("/internal/v1/agents", internal(internal_agents)),
        Route("/internal/v1/tasks/eval-run", internal(task_eval_run), methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.gov = gov
    return app


def main() -> None:  # pragma: no cover
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
