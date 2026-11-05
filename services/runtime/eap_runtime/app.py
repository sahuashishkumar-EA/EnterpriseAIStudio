"""Agent Runtime (data plane).

  POST /v1/agents/{name}@{alias}/invoke     sync | async (Cloud Tasks) | stream (SSE)
  GET  /v1/runs/{id}                        run status / result
  POST /v1/runs/{id}/resume                 human_in_the_loop decision
  POST /v1/knowledge/{source}/documents     ingest (chunk + embed + store)
  POST /v1/knowledge/{source}/search        debug search
  POST /mcp                                 MCP server exposing agents as tools
  POST /internal/v1/execute                 governor evals of drafts (governor only)
  POST /internal/v1/events                  Pub/Sub push: cache invalidation
  POST /internal/v1/tasks/run               Cloud Tasks: async runs (runtime only)

The runtime has no access to governor tables. Definitions + effective policy come from
the governor's resolve API (cached, ETag-revalidated, invalidated by events).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from eap_core.auth import Authenticator, AuthError, Principal
from eap_core.context import HEADER as CTX_HEADER
from eap_core.context import CallContext, CallContextError
from eap_core.contracts import InvokeRequest, InvokeResponse, ResumeRequest
from eap_core.db import Database, expand_url
from eap_core.events import parse_push
from eap_core.tasks import LocalTaskQueue, TaskQueue, queue_from_env

from .budgets import Budgets
from .config import Settings
from .executor import Execution, Resume, Services, root_context
from .knowledge import Embedder, KnowledgeStore
from .mcp_server import McpServer
from .models import ModelRegistry
from .resolver import ResolveError, Resolver, pick_release
from .runs import RunStore
from .tools import ToolRunner

log = logging.getLogger("eap.runtime")
HERE = Path(__file__).parent


class Forbidden(Exception):
    pass


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    governor_transport: httpx.AsyncBaseTransport | None = None,
    vertex_transport: httpx.AsyncBaseTransport | None = None,
    tools_transport: httpx.AsyncBaseTransport | None = None,
    tasks: TaskQueue | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    db = db or Database(expand_url(settings.database_url))
    db.migrate(HERE / "migrations")
    auth = Authenticator(settings.auth)
    resolver = Resolver(settings.governor_url, settings.governor_auth, transport=governor_transport,
                        ttl_s=settings.resolver_ttl_s, max_stale_s=settings.resolver_max_stale_s)
    svc = Services(
        resolver=resolver,
        models=ModelRegistry(settings.gcp_project, transport=vertex_transport),
        tools=ToolRunner(transport=tools_transport, project=settings.gcp_project),
        knowledge=KnowledgeStore(db, Embedder(settings.gcp_project, settings.embedding_location, transport=vertex_transport)),
        budgets=Budgets(db),
        runs=RunStore(db),
    )
    queue = tasks or queue_from_env()
    key = settings.call_context_key

    # ------------------------------------------------------------------- helpers

    def service_name(p: Principal) -> str | None:
        if p.kind != "service":
            return None
        return auth.service_name(p) or (p.sub if p.mode == "dev" else None)

    def require_service(p: Principal, allowed: list[str]) -> None:
        if service_name(p) not in allowed:
            raise Forbidden("this endpoint is restricted to platform services")

    def may_invoke(p: Principal, definition: dict[str, Any], owner_bu: str) -> bool:
        if service_name(p) in settings.trusted_services:
            return True
        roles = p.bu_roles()
        for tok in definition.get("exposure", {}).get("invokers", ["owner_bu"]):
            if tok == "authenticated":
                return True
            if tok == "owner_bu" and (owner_bu in roles or "*" in roles):
                return True
            if tok.startswith("bu:") and (tok[3:] in roles or "*" in roles):
                return True
            if tok.startswith("user:") and p.id == tok[5:]:
                return True
        return False

    async def principal(request: Request) -> Principal:
        return await auth.authenticate(request.headers)

    def wrap(fn):
        async def handler(request: Request) -> Response:
            try:
                return await fn(request)
            except AuthError as e:
                return _error(e.status, "unauthenticated", str(e))
            except Forbidden as e:
                return _error(403, "forbidden", str(e))
            except ResolveError as e:
                return _error(e.status, "resolve_failed", str(e))
            except CallContextError as e:
                return _error(409, e.code, str(e))
            except ValidationError as e:
                return _error(422, "invalid_request", str(e).splitlines()[0])
            except (ValueError, KeyError) as e:
                return _error(400, "bad_request", str(e))

        return handler

    async def run_execution(ex: Execution, input: dict[str, Any], *, principal_id: str, exists: bool = False,
                            idempotency_key: str | None = None) -> InvokeResponse:
        resp = await ex.run(input)
        try:
            await svc.runs.record_response(
                resp, agent=ex.agent, alias=ex.alias, mode=ex.mode, input=input, principal=principal_id,
                on_behalf_of=ex.ctx.on_behalf_of, bu=ex.owner_bu, depth=ex.ctx.depth, state=ex.state,
                idempotency_key=idempotency_key, exists=exists)
        except Exception:
            log.exception("recording run %s failed", resp.run_id)
        return resp

    # --------------------------------------------------------------------- invoke

    async def prepare(name: str, alias: str, p: Principal, req: InvokeRequest, ctx_token: str | None):
        parent: CallContext | None = None
        if ctx_token:
            if service_name(p) not in settings.trusted_services:
                raise Forbidden("call context may only be forwarded by platform services")
            parent = CallContext.verify(ctx_token, key)
        bundle = await resolver.resolve(name, alias)
        routing_key = req.session_id or (parent.trace_id if parent else uuid.uuid4().hex)
        rel = pick_release(bundle, routing_key)
        d = rel["definition"]
        if parent is not None:
            if not d.get("exposure", {}).get("callable_by_agents", True):
                raise Forbidden(f"agent {name} is not callable by agents")
            budget = min(parent.budget_usd, req.budget_usd or float("inf"), float(rel["limits"]["max_cost_usd"]))
            ctx = parent.child(name, budget, max_depth=int(rel["limits"]["max_depth"]))
        else:
            if not may_invoke(p, d, bundle["owner_bu"]):
                raise Forbidden(f"{p.id} may not invoke {name}")
            ctx = root_context(principal=p.id, roles=p.roles, limits=rel["limits"], agent=name,
                               budget_usd=req.budget_usd, trace_id=routing_key if not req.session_id else None)
        return bundle, rel, ctx

    async def invoke(request: Request) -> Response:
        ref = request.path_params["ref"]
        name, _, alias = ref.partition("@")
        alias = alias or settings.default_alias
        p = await principal(request)
        raw = await request.body()
        req = InvokeRequest.model_validate_json(raw) if raw.strip() else InvokeRequest()
        if req.idempotency_key:
            existing = await svc.runs.by_idempotency_key(f"{p.id}:{req.idempotency_key}")
            if existing:
                return JSONResponse(run_view(existing), status_code=200 if existing["status"] not in ("queued", "running") else 202)
        bundle, rel, ctx = await prepare(name, alias, p, req, request.headers.get(CTX_HEADER))
        idem = f"{p.id}:{req.idempotency_key}" if req.idempotency_key else None

        if req.mode == "async":
            await svc.runs.create({
                "id": ctx.run_id, "agent": name, "alias": alias, "version": rel["version"],
                "content_hash": rel["content_hash"], "mode": "async", "status": "queued", "input": req.input,
                "trace_id": ctx.trace_id, "parent_run_id": ctx.parent_run_id, "principal": p.id,
                "on_behalf_of": ctx.on_behalf_of, "bu": bundle["owner_bu"], "depth": ctx.depth,
                "idempotency_key": idem,
            })
            await queue.enqueue("/internal/v1/tasks/run", {
                "run_id": ctx.run_id, "agent": name, "alias": alias, "version": rel["version"],
                "input": req.input, "context": ctx.sign(key, ttl_s=24 * 3600), "principal": p.id,
            }, task_id=ctx.run_id)
            return JSONResponse({"run_id": ctx.run_id, "status": "queued", "trace_id": ctx.trace_id,
                                 "links": {"self": f"/v1/runs/{ctx.run_id}"}}, status_code=202)

        if req.mode == "stream":
            events: asyncio.Queue = asyncio.Queue()

            async def emitter(ev: str, data: dict[str, Any]) -> None:
                await events.put((ev, data))

            ex = Execution(svc, rel, bundle["policy"], ctx, agent=name, alias=alias, owner_bu=bundle["owner_bu"],
                           emitter=emitter, mode="stream")

            async def runner() -> None:
                try:
                    await run_execution(ex, req.input, principal_id=p.id, idempotency_key=idem)
                except Exception as e:  # pragma: no cover - run() already catches
                    await events.put(("error", {"message": str(e)}))

            task = asyncio.create_task(runner())

            async def gen():
                yield f"event: run\ndata: {json.dumps({'run_id': ctx.run_id, 'trace_id': ctx.trace_id, 'agent': name, 'version': rel['version']})}\n\n"
                while True:
                    ev, data = await events.get()
                    yield f"event: {ev}\ndata: {json.dumps(data, default=str)}\n\n"
                    if ev in ("result", "error"):
                        break
                await task

            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        ex = Execution(svc, rel, bundle["policy"], ctx, agent=name, alias=alias, owner_bu=bundle["owner_bu"],
                       mode="sync")
        resp = await run_execution(ex, req.input, principal_id=p.id, idempotency_key=idem)
        return JSONResponse(resp.model_dump(mode="json"))

    def run_view(row: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "agent", "alias", "version", "content_hash", "mode", "status", "output", "error", "trace_id",
                "parent_run_id", "on_behalf_of", "depth", "cost_usd", "input_tokens", "output_tokens", "latency_ms",
                "steps", "sources", "needs_input", "created_at", "updated_at")
        out = {k: row.get(k) for k in keys}
        out["run_id"] = row["id"]
        return out

    def can_see(p: Principal, row: dict[str, Any]) -> bool:
        return (p.id in (row["principal"], row["on_behalf_of"]) or service_name(p) in settings.trusted_services
                or "platform_admin" in p.bu_roles().get("*", set()))

    async def get_run(request: Request) -> Response:
        p = await principal(request)
        row = await svc.runs.get(request.path_params["run_id"])
        if row is None or not can_see(p, row):
            return _error(404, "not_found", "run not found")
        return JSONResponse(run_view(row))

    async def resume(request: Request) -> Response:
        p = await principal(request)
        row = await svc.runs.get(request.path_params["run_id"])
        if row is None or not can_see(p, row):
            return _error(404, "not_found", "run not found")
        if row["status"] != "needs_input":
            return _error(409, "not_waiting", f"run is {row['status']}")
        req = ResumeRequest.model_validate_json(await request.body())
        bundle = await resolver.resolve(row["agent"], row["version"])  # same immutable version
        rel = bundle["primary"]
        ctx = root_context(principal=row["on_behalf_of"], roles=p.roles, limits=rel["limits"], agent=row["agent"],
                           trace_id=row["trace_id"], run_id=row["id"])
        ex = Execution(svc, rel, bundle["policy"], ctx, agent=row["agent"], alias=row["alias"],
                       owner_bu=bundle["owner_bu"], mode="sync", resume=Resume(input=req.input, state=row["state"] or {}))
        resp = await ex.run(row["input"])
        resp.cost.usd += row["cost_usd"] or 0.0
        await svc.runs.record_response(resp, agent=row["agent"], alias=row["alias"], mode=row["mode"], input=row["input"],
                                       principal=row["principal"], on_behalf_of=row["on_behalf_of"], bu=row["bu"],
                                       depth=row["depth"], state=ex.state, exists=True)
        return JSONResponse(resp.model_dump(mode="json"))

    # -------------------------------------------------------------------- internal

    async def task_run(request: Request) -> Response:
        p = await principal(request)
        require_service(p, settings.task_callers + [settings.service_name])
        b = await request.json()
        row = await svc.runs.get(b["run_id"])
        if row is None or row["status"] not in ("queued", "running"):
            return JSONResponse({"skipped": True})  # idempotent redelivery
        ctx = CallContext.verify(b["context"], key)
        bundle = await resolver.resolve(b["agent"], b["version"])
        rel = bundle["primary"]
        await svc.runs.update(b["run_id"], status="running")
        ex = Execution(svc, rel, bundle["policy"], ctx, agent=b["agent"], alias=b.get("alias"),
                       owner_bu=bundle["owner_bu"], mode="async")
        resp = await run_execution(ex, b["input"], principal_id=b["principal"], exists=True)
        return JSONResponse({"run_id": resp.run_id, "status": resp.status})

    if isinstance(queue, LocalTaskQueue):
        async def local_task(payload: dict[str, Any]) -> None:
            row = await svc.runs.get(payload["run_id"])
            if row is None or row["status"] not in ("queued", "running"):
                return
            ctx = CallContext.verify(payload["context"], key)
            bundle = await resolver.resolve(payload["agent"], payload["version"])
            await svc.runs.update(payload["run_id"], status="running")
            ex = Execution(svc, bundle["primary"], bundle["policy"], ctx, agent=payload["agent"],
                           alias=payload.get("alias"), owner_bu=bundle["owner_bu"], mode="async")
            await run_execution(ex, payload["input"], principal_id=payload["principal"], exists=True)

        queue.register("/internal/v1/tasks/run", local_task)

    async def execute(request: Request) -> Response:
        """Governor evals: run a draft (or version) definition supplied in the request."""
        p = await principal(request)
        require_service(p, settings.execute_callers)
        b = await request.json()
        rel, policy = b["release"], b.get("policy") or {}
        agent = b.get("agent") or rel["definition"]["name"]
        ctx = root_context(principal=b.get("principal") or p.id, roles=[], limits=rel["limits"], agent=agent,
                           attributes={"eval": True})
        ex = Execution(svc, rel, policy, ctx, agent=agent, alias=None, owner_bu=rel["definition"].get("owner_bu"),
                       mode="execute")
        resp = await run_execution(ex, b.get("input") or {}, principal_id=p.id)
        return JSONResponse(resp.model_dump(mode="json"))

    async def events(request: Request) -> Response:
        p = await principal(request)
        require_service(p, settings.event_callers)
        ev = parse_push(await request.json())
        agent = ev.data.get("agent")
        if ev.event_type in ("alias.moved", "version.published", "version.flagged") and agent:
            n = resolver.invalidate(agent)
        else:
            n = resolver.invalidate(None)
        mcp.invalidate()
        log.info("event %s: invalidated %d cache entries", ev.event_type, n)
        return JSONResponse({"ok": True, "invalidated": n})  # 2xx acks the Pub/Sub message

    # ------------------------------------------------------------------- knowledge

    async def knowledge_source(source_id: str) -> dict[str, Any]:
        # Ingestion is keyed by the catalog knowledge id; the ingesting service passes the
        # embedding model (it must match the catalog entry the agents' bundles carry).
        return {"id": source_id, "status": "active", "config": {}}

    async def ingest(request: Request) -> Response:
        p = await principal(request)
        if service_name(p) not in settings.trusted_services and not (
            "platform_admin" in p.bu_roles().get("*", set()) or any("knowledge_writer" in r for r in p.bu_roles().values())
        ):
            raise Forbidden("knowledge_writer role or platform service required")
        b = await request.json()
        source = await knowledge_source(request.path_params["source"])
        source["config"] = {"embedding_model": b.get("embedding_model", "mock-embedding")}
        docs = b.get("documents") or []
        if not isinstance(docs, list) or not docs:
            raise ValueError("documents must be a non-empty list")
        return JSONResponse(await svc.knowledge.ingest(source, docs), status_code=201)

    async def search(request: Request) -> Response:
        p = await principal(request)
        if service_name(p) not in settings.trusted_services and "*" not in p.bu_roles():
            raise Forbidden("platform admin or service only")
        b = await request.json()
        source = await knowledge_source(request.path_params["source"])
        source["config"] = {"embedding_model": b.get("embedding_model", "mock-embedding")}
        hits = await svc.knowledge.search(source, b["query"], int(b.get("top_k", 4)))
        return JSONResponse({"hits": hits})

    # ------------------------------------------------------------------------- mcp

    def mcp_invoker(p: Principal):
        async def inv(name: str, args: dict[str, Any]) -> dict[str, Any]:
            try:
                bundle, rel, ctx = await prepare(name, settings.mcp_alias, p, InvokeRequest(input=args), None)
            except (Forbidden, CallContextError, ResolveError) as e:
                return {"status": "failed", "error": {"message": str(e)}}
            ex = Execution(svc, rel, bundle["policy"], ctx, agent=name, alias=settings.mcp_alias,
                           owner_bu=bundle["owner_bu"], mode="mcp")
            return (await run_execution(ex, args, principal_id=p.id)).model_dump(mode="json")

        return inv

    mcp = McpServer(lambda: resolver.list_exposed(settings.mcp_alias), settings.mcp_allowed_origins)

    async def mcp_endpoint(request: Request) -> Response:
        if request.method == "GET":
            return Response(status_code=405, headers={"Allow": "POST"})
        try:
            p = await principal(request)
        except AuthError:
            return await mcp.handle(request, None)
        return await mcp.handle(request, mcp_invoker(p))

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "runtime", "resolver": resolver.stats})

    routes = [
        Route("/healthz", healthz),
        Route("/v1/agents/{ref}/invoke", wrap(invoke), methods=["POST"]),
        Route("/v1/runs/{run_id}", wrap(get_run)),
        Route("/v1/runs/{run_id}/resume", wrap(resume), methods=["POST"]),
        Route("/v1/knowledge/{source}/documents", wrap(ingest), methods=["POST"]),
        Route("/v1/knowledge/{source}/search", wrap(search), methods=["POST"]),
        Route("/mcp", wrap(mcp_endpoint), methods=["GET", "POST"]),
        Route("/internal/v1/execute", wrap(execute), methods=["POST"]),
        Route("/internal/v1/events", wrap(events), methods=["POST"]),
        Route("/internal/v1/tasks/run", wrap(task_run), methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.svc = svc
    app.state.settings = settings
    app.state.tasks = queue
    app.state.db = db
    return app


def main() -> None:  # pragma: no cover
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

