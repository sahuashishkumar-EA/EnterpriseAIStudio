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
from eap_core.db import Database, dumps, expand_url
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
from .websearch import WebSearcher
from .webscan import WebScanner

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
    web_transport: httpx.AsyncBaseTransport | None = None,
    tasks: TaskQueue | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    db = db or Database(expand_url(settings.database_url))
    with db.startup_lock("eap-runtime-startup"):
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
        web=WebSearcher(project=settings.gcp_project, location=settings.web_search_location, transport=web_transport),
    )
    queue = tasks or queue_from_env()
    scanner = WebScanner(transport=web_transport, ssrf_protection=settings.web_ssrf_protection)
    key = settings.call_context_key

    # ------------------------------------------------------------------- helpers

    def service_name(p: Principal) -> str | None:
        if p.kind != "service":
            return None
        return auth.service_name(p) or (p.sub if p.mode == "dev" else None)

    def require_service(p: Principal, allowed: list[str]) -> None:
        if service_name(p) not in allowed:
            raise Forbidden("this endpoint is restricted to platform services")

    def user_profile(p: Principal, req: InvokeRequest) -> dict[str, Any] | None:
        """Only trusted front ends (delegating services) may describe the user to the agent."""
        if req.user is None or p.mode != "delegated":
            return None
        return req.user.model_dump()

    def may_invoke(p: Principal, definition: dict[str, Any], owner_bu: str) -> bool:
        if service_name(p) in settings.trusted_services:
            return True
        if p.mode == "delegated":  # end user authenticated by a front-end service (chat app)
            if not definition.get("exposure", {}).get("chat"):
                return False
            bu = (p.claims or {}).get("bu")
            if not bu:  # front end did not say which BU the user acts for
                return True
            invokers = definition.get("exposure", {}).get("invokers", ["owner_bu"])
            return owner_bu == bu or "authenticated" in invokers or f"bu:{bu}" in invokers
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

    async def caller(request: Request) -> Principal:
        """The authenticated caller, or the end user a delegating front-end acts for."""
        p = await principal(request)
        user = request.headers.get("x-eap-on-behalf-of")
        if user:
            if service_name(p) not in settings.delegating_services:
                raise Forbidden("only delegating front-end services may act on behalf of a user")
            bu = (request.headers.get("x-eap-user-bu") or "").strip()
            return Principal(sub=user, email=user if "@" in user else None, kind="user", mode="delegated",
                             claims={"delegated_by": service_name(p), **({"bu": bu} if bu else {})})
        return p

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
        platform = (d.get("hosting") or {}).get("platform", "internal")
        if platform != "internal":
            raise Forbidden(f"agent {name} runs on {platform}; reach it through that platform "
                            "(the platform governs it via /gov/v1)")
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
        p = await caller(request)
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
                "fcontext": req.context, "history": [t.model_dump() for t in req.history],
                "user": user_profile(p, req),
            }, task_id=ctx.run_id)
            return JSONResponse({"run_id": ctx.run_id, "status": "queued", "trace_id": ctx.trace_id,
                                 "links": {"self": f"/v1/runs/{ctx.run_id}"}}, status_code=202)

        if req.mode == "stream":
            events: asyncio.Queue = asyncio.Queue()

            async def emitter(ev: str, data: dict[str, Any]) -> None:
                await events.put((ev, data))

            ex = Execution(svc, rel, bundle["policy"], ctx, agent=name, alias=alias, owner_bu=bundle["owner_bu"],
                           emitter=emitter, mode="stream", context_id=req.context,
                           history=[t.model_dump() for t in req.history], user=user_profile(p, req))

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
                       mode="sync", context_id=req.context, history=[t.model_dump() for t in req.history],
                       user=user_profile(p, req))
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

    async def feedback(request: Request) -> Response:
        """End-user 👍/👎 on an answer (the chat app sends it on the user's behalf)."""
        p = await caller(request)
        row = await svc.runs.get(request.path_params["run_id"])
        if row is None or not can_see(p, row):
            return _error(404, "not_found", "run not found")
        b = await request.json()
        rating = b.get("rating")
        if rating not in ("up", "down"):
            return _error(400, "invalid", "rating must be 'up' or 'down'")
        await svc.runs.feedback(row["id"], rating, str(b.get("comment") or ""), p.id)
        return JSONResponse({"ok": True, "run_id": row["id"], "rating": rating})

    # ------------------------------------------------------- ops (governor only)

    def ops_view(row: dict[str, Any], full: bool = False) -> dict[str, Any]:
        out = run_view(row) if full else {k: row.get(k) for k in (
            "id", "agent", "alias", "version", "mode", "status", "trace_id", "parent_run_id", "on_behalf_of",
            "depth", "cost_usd", "latency_ms", "created_at")}
        text = row.get("input")
        if isinstance(text, dict):
            text = text.get("message") or text.get("question") or text.get("query") or dumps(text)
        out.update({
            "run_id": row["id"], "bu": row.get("bu"), "principal": row.get("principal"),
            "context_id": row.get("context_id"), "flags": row.get("flags") or [], "feedback": row.get("feedback"),
            "review_status": row.get("review_status"), "assignee": row.get("assignee"),
            "review_note": row.get("review_note"), "reviewed_by": row.get("reviewed_by"),
            "reviewed_at": row.get("reviewed_at"), "input_text": str(text or "")[:300],
            "error_code": (row.get("error") or {}).get("code") if isinstance(row.get("error"), dict) else None,
        })
        if full:
            out["input"] = row.get("input")
        else:
            o = row.get("output")
            out["output_text"] = (o if isinstance(o, str) else dumps(o) if o is not None else "")[:300]
        return out

    def csv_param(request: Request, name: str) -> list[str] | None:
        v = request.query_params.get(name)
        return None if v is None else [x for x in v.split(",") if x]

    async def ops_runs(request: Request) -> Response:
        p = await principal(request)
        require_service(p, settings.execute_callers)
        qp = request.query_params
        rows = await svc.runs.search(
            agents=csv_param(request, "agent"), bus=csv_param(request, "bus"), status=qp.get("status"),
            flag=qp.get("flag"), review=qp.get("review"), feedback=qp.get("feedback"), q=qp.get("q"),
            since=qp.get("since"), include_tests=qp.get("include_tests") == "true",
            limit=int(qp.get("limit", 50)), offset=int(qp.get("offset", 0)))
        return JSONResponse({"runs": [ops_view(r) for r in rows]})

    async def ops_run(request: Request) -> Response:
        p = await principal(request)
        require_service(p, settings.execute_callers)
        row = await svc.runs.get(request.path_params["run_id"])
        if row is None:
            return _error(404, "not_found", "run not found")
        out = ops_view(row, full=True)
        rows = await svc.runs.search(q=row["trace_id"], include_tests=True, limit=50)
        out["related"] = [ops_view(r) for r in rows if r["id"] != row["id"]]
        return JSONResponse(out)

    async def ops_review(request: Request) -> Response:
        p = await principal(request)
        require_service(p, settings.execute_callers)
        b = await request.json()
        if b.get("status") not in (None, "open", "resolved"):
            return _error(400, "invalid", "status must be open or resolved")
        if await svc.runs.get(request.path_params["run_id"]) is None:
            return _error(404, "not_found", "run not found")
        await svc.runs.review(request.path_params["run_id"], status=b.get("status"), assignee=b.get("assignee"),
                              note=b.get("note"), by=str(b.get("actor") or p.id))
        return JSONResponse(ops_view(await svc.runs.get(request.path_params["run_id"]), full=True))  # type: ignore[arg-type]

    async def ops_stats(request: Request) -> Response:
        """Per-agent volume, outcomes, cost, latency and feedback over the last N days (live traffic only)."""
        from datetime import datetime, timedelta, timezone

        p = await principal(request)
        require_service(p, settings.execute_callers)
        days = max(1, min(int(request.query_params.get("days", 14)), 90))
        since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%dT00:00:00")
        rows = await svc.runs.search(agents=csv_param(request, "agent"), bus=csv_param(request, "bus"),
                                     since=since, limit=500, offset=0)
        more = rows
        while len(more) == 500 and len(rows) < 20000:
            more = await svc.runs.search(agents=csv_param(request, "agent"), bus=csv_param(request, "bus"),
                                         since=since, limit=500, offset=len(rows))
            rows += more
        day_keys = [(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
        agents: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["mode"] == "child":
                continue
            a = agents.setdefault(r["agent"], {"agent": r["agent"], "bu": r.get("bu"), "runs": 0, "ok": 0, "failed": 0,
                                               "partial": 0, "needs_input": 0, "escalated": 0, "refused": 0,
                                               "cost_usd": 0.0, "lat": [], "up": 0, "down": 0, "open": 0,
                                               "contexts": {}, "days": {d: {"runs": 0, "failed": 0, "cost_usd": 0.0}
                                                                        for d in day_keys}})
            a["runs"] += 1
            a[r["status"]] = a.get(r["status"], 0) + (1 if r["status"] in ("ok", "failed", "partial", "needs_input") else 0)
            flags = r.get("flags") or []
            a["escalated"] += "escalated" in flags
            a["refused"] += any(f.startswith("responded:") for f in flags)
            a["cost_usd"] += r["cost_usd"] or 0
            a["lat"].append(r["latency_ms"] or 0)
            fb = r.get("feedback") or {}
            a["up"] += fb.get("rating") == "up"
            a["down"] += fb.get("rating") == "down"
            a["open"] += r.get("review_status") == "open"
            if r.get("context_id"):
                a["contexts"][r["context_id"]] = a["contexts"].get(r["context_id"], 0) + 1
            d = a["days"].get(r["created_at"][:10])
            if d:
                d["runs"] += 1
                d["failed"] += r["status"] == "failed"
                d["cost_usd"] += r["cost_usd"] or 0
        out = []
        for a in agents.values():
            lat = sorted(a.pop("lat"))
            pct = (lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0)
            a.update(p50_ms=pct(0.5), p95_ms=pct(0.95), cost_usd=round(a["cost_usd"], 6),
                     days=[{"day": k, **{kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
                           for k, v in a["days"].items()])
            out.append(a)
        out.sort(key=lambda x: -x["runs"])
        return JSONResponse({"days": days, "since": since, "agents": out})

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
                       owner_bu=bundle["owner_bu"], mode="async", context_id=b.get("fcontext"),
                       history=b.get("history") or [], user=b.get("user"))
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
                           alias=payload.get("alias"), owner_bu=bundle["owner_bu"], mode="async",
                           context_id=payload.get("fcontext"), history=payload.get("history") or [],
                           user=payload.get("user"))
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
                       mode="execute", context_id=b.get("context"), history=b.get("history") or [],
                       user=b.get("user"))
        resp = await run_execution(ex, b.get("input") or {}, principal_id=p.id)
        return JSONResponse(resp.model_dump(mode="json"))

    async def knowledge_copy(request: Request) -> Response:
        """Governor snapshots a draft web index for a published version."""
        p = await principal(request)
        require_service(p, settings.execute_callers)
        b = await request.json()
        n = await svc.knowledge.copy_source(b["from"], b["to"])
        return JSONResponse({"copied_chunks": n, "to": b["to"]})

    async def knowledge_scan(request: Request) -> Response:
        """Governor asks the runtime to (re)build a functional context's web knowledge."""
        p = await principal(request)
        require_service(p, settings.execute_callers)
        b = await request.json()
        source_id, sources = b["source_id"], b.get("sources") or []
        docs, report = await scanner.scan(sources, b.get("hints") or [])
        model = b.get("embedding_model") or "mock-embedding"
        warnings: list[str] = []
        await svc.knowledge.delete_source(source_id)
        result = {"documents": 0, "chunks": 0}
        if docs:
            try:
                result = await svc.knowledge.ingest({"id": source_id, "config": {"embedding_model": model}}, docs)
            except Exception as e:  # e.g. embedding model not available in this region
                warnings.append(f"embedding model {model} failed ({str(e)[:150]}); used mock-embedding")
                model = "mock-embedding"
                await svc.knowledge.delete_source(source_id)
                result = await svc.knowledge.ingest({"id": source_id, "config": {"embedding_model": model}}, docs)
        return JSONResponse({"source_id": source_id, "pages": len(docs), "chunks": result["chunks"],
                             "embedding_model": model, "report": report, "warnings": warnings})

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
        Route("/v1/runs/{run_id}/feedback", wrap(feedback), methods=["POST"]),
        Route("/internal/v1/ops/runs", wrap(ops_runs)),
        Route("/internal/v1/ops/runs/{run_id}", wrap(ops_run)),
        Route("/internal/v1/ops/runs/{run_id}/review", wrap(ops_review), methods=["POST"]),
        Route("/internal/v1/ops/stats", wrap(ops_stats)),
        Route("/v1/knowledge/{source}/documents", wrap(ingest), methods=["POST"]),
        Route("/v1/knowledge/{source}/search", wrap(search), methods=["POST"]),
        Route("/mcp", wrap(mcp_endpoint), methods=["GET", "POST"]),
        Route("/internal/v1/execute", wrap(execute), methods=["POST"]),
        Route("/internal/v1/events", wrap(events), methods=["POST"]),
        Route("/internal/v1/tasks/run", wrap(task_run), methods=["POST"]),
        Route("/internal/v1/knowledge/scan", wrap(knowledge_scan), methods=["POST"]),
        Route("/internal/v1/knowledge/copy", wrap(knowledge_copy), methods=["POST"]),
    ]
    from . import govapi

    routes += govapi.build_routes(resolver=resolver, runs=svc.runs, settings=settings, caller=caller,
                                  service_name=service_name, outer=wrap)
    if settings.edge:  # public deployment for external platforms (e.g. Copilot Studio)
        routes = [r for r in routes if r.path in ("/healthz", "/mcp") or r.path.startswith("/gov/v1/")]
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

