"""C03 Registry API (spec 7.1). REST over the C04 registry, OpenAPI 3.1 at /openapi.json.

Authorisation (C03 non-functional): the caller's roles are its token roles ("scope:role") plus
role bindings in the registry for the artifact's scopes (business unit, agent:<id>, '*').
Field-level rules come from the metamodel's x-editors annotations (C01-F3). Every call is
written to the audit log.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from eap_core.auth import AuthConfig, AuthError, Authenticator, Principal
from eap_core.db import Database, Tx
from eap_metamodel import Metamodel, migrate
from eap_store import store
from eap_store.store import Forbidden, Invalid, RegistryError

log = logging.getLogger("eap.registry")

Handler = Callable[[Request, Principal], Awaitable[Any]]


def create_app(db: Database, mm: Metamodel, engine: Any, authenticator: Authenticator | None = None,
               provisioner: Any = None, internal_principals: set[str] | None = None) -> Starlette:
    auth = authenticator or Authenticator(AuthConfig.from_env())
    internal_ok = internal_principals  # None = any authenticated service (dev); set on GCP
    ROUTES: list[tuple[str, str, str]] = []  # (method, path, summary) for the OpenAPI document

    def token_roles(p: Principal, scopes: list[str]) -> set[str]:
        out = set()
        for bu, roles in p.bu_roles().items():
            if bu == "*" or bu in scopes:
                out |= roles
        return out

    def roles_for(tx: Tx, p: Principal, scopes: list[str]) -> set[str]:
        return token_roles(p, scopes) | store.roles_for(tx, p.id, scopes)

    def endpoint(method: str, path: str, summary: str, internal: bool = False):
        ROUTES.append((method, path, summary))

        def deco(fn: Handler):
            async def wrapper(request: Request):
                try:
                    p = await auth.authenticate(request.headers)
                    if internal and internal_ok is not None and p.id not in internal_ok:
                        raise Forbidden("internal endpoint")
                    result = await fn(request, p)
                    status = 200
                    if isinstance(result, tuple):
                        status, result = result
                    await db.call(lambda tx: store.audit(tx, p.id, f"api.{method} {path}", request.url.path, None,
                                                         {"status": status}))
                    return JSONResponse(result, status)
                except AuthError as e:
                    return JSONResponse({"error": "unauthenticated", "message": str(e)}, e.status)
                except RegistryError as e:
                    return JSONResponse({"error": e.code, "message": str(e), "details": e.details}, e.status)

            return Route(path, wrapper, methods=[method])

        return deco

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            raise Invalid("request body must be JSON") from None
        if not isinstance(data, dict):
            raise Invalid("request body must be an object")
        return data

    routes: list[Route] = []

    def add(r):
        routes.append(r)
        return r

    # -------------------------------------------------------------- artifacts and versions

    @add
    @endpoint("GET", "/artifacts", "List or search artifacts (?type=&q=)")
    async def artifacts_list(request, p):
        q = request.query_params
        return {"items": await db.call(lambda tx: store.list_artifacts(tx, q.get("type"), q.get("q")))}

    def check_edit(tx: Tx, p: Principal, type_: str, old: Any, new: Any, art: dict[str, Any]) -> None:
        paths = mm.classify(type_, old, new).paths
        roles = roles_for(tx, p, store.scopes_of(art, new))
        if "platform_admin" in roles:
            return
        bad = mm.edit_violations(type_, paths, roles)
        if bad:
            raise Forbidden("your roles may not edit these fields", bad)

    @add
    @endpoint("POST", "/artifacts", "Create an artifact with its first draft")
    async def artifacts_create(request, p):
        b = await body(request)
        for k in ("type", "name", "content"):
            if k not in b:
                raise Invalid(f"{k} is required")

        def run(tx: Tx):
            check_edit(tx, p, b["type"], None, b["content"], {"type": b["type"]})
            return store.create_artifact(tx, mm, b["type"], b["name"], b.get("owner") or p.id, b["content"], p.id)

        out = await db.call(run)
        await engine._emit("config.draft.created", {"id": None, "version_ids": [out["version"]["id"]],
                                                    "environment": "dev"})
        return 201, out

    @add
    @endpoint("GET", "/artifacts/{id}", "Read an artifact with its latest version and active pointers")
    async def artifact_get(request, p):
        aid = request.path_params["id"]

        def run(tx: Tx):
            art = store.get_artifact(tx, aid)
            active = {e: (v := store.active_version(tx, aid, e)) and v["id"] for e in store.ENVIRONMENTS}
            return {**art, "latest": store.latest_version(tx, aid), "active": active}

        return await db.call(run)

    @add
    @endpoint("GET", "/artifacts/{id}/versions", "Version history")
    async def versions(request, p):
        return {"items": await db.call(lambda tx: store.list_versions(tx, request.path_params["id"]))}

    @add
    @endpoint("GET", "/versions/{versionId}", "Read a version (hash verified, C03-F4; ?upgrade=true migrates)")
    async def version_get(request, p):
        def run(tx: Tx):
            v = store.get_version(tx, request.path_params["versionId"])
            if request.query_params.get("upgrade") == "true":
                art = store.get_artifact(tx, v["artifact_id"])
                cur = mm.schema_version(art["type"])
                if v["schema_version"] != cur:
                    v = {**v, "content": migrate(art["type"], v["content"], v["schema_version"], cur),
                         "schema_version": cur, "upgraded_from": v["schema_version"]}
            return v

        return await db.call(run)

    @add
    @endpoint("POST", "/artifacts/{id}/drafts", "Create a new draft; body must include baseVersionId (C03-F3)")
    async def drafts_create(request, p):
        b = await body(request)
        aid = request.path_params["id"]
        if "content" not in b:
            raise Invalid("content is required")
        if not b.get("baseVersionId"):  # C03-F3, before any other check
            raise Invalid("baseVersionId is required (optimistic locking)")

        def run(tx: Tx):
            art = store.get_artifact(tx, aid)
            base = store.get_version(tx, b["baseVersionId"])
            check_edit(tx, p, art["type"], base and base["content"], b["content"], art)
            return store.create_draft(tx, mm, aid, b.get("baseVersionId"), b["content"], p.id)

        v = await db.call(run)
        await engine._emit("config.draft.created", {"id": None, "version_ids": [v["id"]], "environment": "dev"})
        return 201, v

    @add
    @endpoint("GET", "/versions/{a}/diff/{b}", "JSON diff between two versions")
    async def diff(request, p):
        return await db.call(lambda tx: store.diff(tx, request.path_params["a"], request.path_params["b"]))

    @add
    @endpoint("GET", "/artifacts/{id}/dependents", "Which artifacts reference this one (C03-F5)")
    async def dependents(request, p):
        return {"items": await db.call(lambda tx: store.dependents(tx, mm, request.path_params["id"]))}

    @add
    @endpoint("POST", "/artifacts/{id}/rollback", "Reactivate an earlier version (C02-F6, C05-F7)")
    async def rollback(request, p):
        b = await body(request)
        aid = request.path_params["id"]
        env = b.get("environment", "production")

        def allowed(tx: Tx):
            v = store.active_version(tx, aid, env)
            art = store.get_artifact(tx, aid)
            roles = roles_for(tx, p, store.scopes_of(art, v["content"] if v else {}))
            need = {"platform_admin", "platform", "risk", "bu_owner", "business_owner"}
            if not roles & need:
                raise Forbidden(f"rollback needs one of {sorted(need)}")

        await db.call(allowed)
        return await engine.rollback(aid, env, p.id, b.get("versionId"), bool(b.get("emergency")),
                                     b.get("reason") or "rollback")

    # -------------------------------------------------------------- change requests (C05)

    @add
    @endpoint("POST", "/change-requests", "Submit versions for promotion")
    async def cr_create(request, p):
        b = await body(request)
        return 201, await engine.submit(b.get("versionIds") or [], b.get("reason", ""),
                                        b.get("environment", "dev"), p.id)

    @add
    @endpoint("GET", "/change-requests", "Approval inbox: ?state=awaiting_approval&role=risk (C02-F5)")
    async def cr_list(request, p):
        state, role = request.query_params.get("state"), request.query_params.get("role")

        def run(tx: Tx):
            ids = [r["id"] for r in tx.fetchall("SELECT id FROM change_request ORDER BY seq DESC LIMIT 200")]
            out = []
            for i in ids:
                cr = engine.get_cr(tx, i)
                if state and cr["state"] != state:
                    continue
                if role and not any(r["role"] == role and p.id in r["eligible"] for r in cr["required_roles"]):
                    continue
                out.append({k: cr[k] for k in ("id", "state", "reason", "risk_tier", "environment", "requested_by",
                                                "created_at", "required_roles")})
            return out

        return {"items": await db.call(run)}

    @add
    @endpoint("GET", "/change-requests/{id}", "Status with checks, evaluations and approvals")
    async def cr_get(request, p):
        return await db.call(lambda tx: engine.get_cr(tx, request.path_params["id"]))

    @add
    @endpoint("POST", "/change-requests/{id}/approvals", "Approve or reject (role-checked, no self-approval)")
    async def cr_approve(request, p):
        b = await body(request)
        return await engine.approve(request.path_params["id"], p.id, b.get("role", ""), b.get("decision", ""),
                                    b.get("comment", ""))

    @add
    @endpoint("POST", "/change-requests/{id}/promote", "Promote an activated change to test or production")
    async def cr_promote(request, p):
        b = await body(request)
        cr = await db.call(lambda tx: engine.get_cr(tx, request.path_params["id"]))

        def allowed(tx: Tx):
            roles = token_roles(p, ["*"]) | store.roles_for(tx, p.id, ["*"])
            if not roles & {"platform", "platform_admin"} and p.id != cr["requested_by"]:
                raise Forbidden("only the requester or the platform team promotes")

        await db.call(allowed)
        return await engine.promote(cr["id"], b.get("environment", ""), p.id)

    # -------------------------------------------------------------- schemas, audit, roles

    @add
    @endpoint("GET", "/schemas", "List metamodel schemas")
    async def schemas(request, p):
        return {"items": [{"type": t, "schemaVersion": mm.schema_version(t)} for t in mm.schemas]}

    @add
    @endpoint("GET", "/schemas/{type}/{schemaVersion}", "Serve a metamodel schema to the console")
    async def schema_get(request, p):
        t, ver = request.path_params["type"], request.path_params["schemaVersion"]
        s = mm.schema(t)
        if ver not in ("latest", s["x-schema-version"]):
            raise store.NotFound(f"{t} schema {ver} not served (current {s['x-schema-version']})")
        return s

    @add
    @endpoint("GET", "/audit", "Audit log (?target=)")
    async def audit(request, p):
        return {"items": await db.call(lambda tx: store.list_audit(tx, request.query_params.get("target")))}

    @add
    @endpoint("POST", "/role-bindings", "Name a principal for a role in a scope (platform admins)")
    async def role_bind(request, p):
        b = await body(request)
        if "platform_admin" not in token_roles(p, ["*"]):
            raise Forbidden("platform_admin required")
        await db.call(lambda tx: store.grant(tx, b["principal"], b["role"], b.get("scope", "*"), p.id))
        return 201, {"ok": True}

    @add
    @endpoint("GET", "/deployments", "Deployments recorded by C10/C11 (?environment=)")
    async def deployments(request, p):
        return {"items": await db.call(lambda tx: store.deployments(tx, request.query_params.get("environment")))}

    # -------------------------------------------------------------- internal (service-to-service)

    @add
    @endpoint("GET", "/internal/v1/snapshot", "Active configuration of one environment (for C07)", internal=True)
    async def snapshot(request, p):
        env = request.query_params.get("environment", "production")
        return {"environment": env, "snapshot": await db.call(lambda tx: store.snapshot(tx, env))}

    @add
    @endpoint("POST", "/internal/v1/config-reads", "Runtime configuration reads (C07/C08 audit)", internal=True)
    async def config_reads(request, p):
        b = await body(request)
        await db.call(lambda tx: store.log_config_read(tx, b["agentIdentity"], b["versions"], b["environment"],
                                                       b.get("source", "registry")))
        return 202, {"ok": True}

    @add
    @endpoint("POST", "/internal/v1/tasks/checks", "Workflow task: run checks", internal=True)
    async def task_checks(request, p):
        return await engine.run_checks((await body(request))["change_request_id"])

    @add
    @endpoint("POST", "/internal/v1/tasks/provision", "Workflow task: provisioning (C11)", internal=True)
    async def task_provision(request, p):
        if provisioner is None:
            raise RegistryError("no provisioner configured")
        return await provisioner.run((await body(request))["change_request_id"])

    @add
    @endpoint("POST", "/internal/v1/reconcile", "Re-create an agent on the platform from the registry", internal=True)
    async def reconcile(request, p):
        b = await body(request)
        if provisioner is None:
            raise RegistryError("no provisioner configured")
        return await provisioner.reconcile(b["agentId"], b.get("environment", "production"), p.id)

    async def openapi(request: Request):
        paths: dict[str, Any] = {}
        for method, path, summary in ROUTES:
            params = [{"name": s.strip("{}"), "in": "path", "required": True, "schema": {"type": "string"}}
                      for s in path.split("/") if s.startswith("{")]
            paths.setdefault(path, {})[method.lower()] = {"summary": summary, "parameters": params,
                                                          "responses": {"200": {"description": "OK"}}}
        return JSONResponse({"openapi": "3.1.0", "info": {"title": "EAP Registry API (C03)", "version": "1.0.0"},
                             "paths": paths})

    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    routes += [Route("/openapi.json", openapi), Route("/healthz", healthz)]
    return Starlette(routes=routes)
