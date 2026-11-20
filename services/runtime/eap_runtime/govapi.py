"""Governance API for agents that run outside the platform (/gov/v1).

An agent built in Microsoft Copilot Studio, or any other platform, stays governed by the Governor:

  GET  /gov/v1/agents/{name}/profile        published behaviour: composed instructions for a functional
                                             context, starters, guardrail summary, limits (ETag/304)
  POST /gov/v1/agents/{name}/check/input    before the external agent answers: allow / respond / block,
                                             plus the redacted text to use (hard controls stay in code)
  POST /gov/v1/agents/{name}/check/output   before the answer is shown: allow / block, redacted output
  POST /gov/v1/agents/{name}/runs           report a finished turn (when the platform cannot call checks)

Every call is recorded as a run (mode "external"), so hand-overs land in the Monitor inbox and the
dashboard covers external agents too. Callers must be listed in the definition's hosting.callers
(service names, OIDC subjects / Entra app ids, or user:<email>), or be trusted platform services.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from eap_core import compose, schema
from eap_core.auth import Principal
from eap_core.db import now_iso

from .guardrails import Enforcer, GuardrailBlocked, PolicyResponse
from .resolver import pick_release


class GovError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def principal_tokens(p: Principal, service_name: str | None) -> set[str]:
    c = p.claims or {}
    toks = {p.id, p.sub, f"user:{p.id}"}
    for k in ("azp", "appid", "client_id", "email"):
        if c.get(k):
            toks.add(str(c[k]))
    if service_name:
        toks.add(service_name)
    return {t for t in toks if t}


def build_routes(*, resolver: Any, runs: Any, settings: Any, caller: Callable[[Request], Awaitable[Principal]],
                 service_name: Callable[[Principal], str | None],
                 outer: Callable[[Any], Any] = lambda f: f) -> list[Route]:

    def err(e: GovError) -> JSONResponse:
        return JSONResponse({"error": {"code": e.code, "message": str(e)}}, status_code=e.status)

    async def load(request: Request, p: Principal) -> tuple[dict[str, Any], dict[str, Any], schema.AgentDefinition]:
        name = request.path_params["name"]
        alias = request.query_params.get("alias") or settings.default_alias
        bundle = await resolver.resolve(name, alias)
        rel = pick_release(bundle, request.headers.get("x-eap-session") or uuid.uuid4().hex)
        defn = schema.parse_definition(rel["definition"])
        sname = service_name(p)
        allowed = sname in settings.trusted_services or bool(principal_tokens(p, sname) & set(defn.hosting.callers)) \
            or "platform_admin" in p.bu_roles().get("*", set())
        if not allowed:
            raise GovError(403, "forbidden", f"{p.id} is not an allowed caller of {name}'s governance API "
                                             "(add it under Technical → Hosting → allowed callers)")
        return bundle, rel, defn

    def context_of(defn: schema.AgentDefinition, ctx_id: str | None) -> schema.FunctionalContext | None:
        try:
            return defn.context(ctx_id)
        except ValueError as e:
            raise GovError(404, "not_found", str(e)) from e

    def enforcer(rel: dict[str, Any], fctx: schema.FunctionalContext | None) -> Enforcer:
        return Enforcer([*(rel.get("guardrails") or []), *compose.functional_guardrails(fctx)])

    def findings(enf: Enforcer) -> list[dict[str, Any]]:
        return [{"guardrail": f.guardrail, "stage": f.stage, "action": f.action, "detail": f.detail} for f in enf.findings]

    def steps(fs: list[dict[str, Any]], status_by_action: dict[str, str] | None = None) -> list[dict[str, Any]]:
        m = status_by_action or {"block": "blocked", "redact": "redacted", "warn": "warned", "respond": "responded"}
        return [{"kind": "guardrail", "name": f["guardrail"], "status": m.get(f["action"], f["action"]),
                 "detail": {"stage": f["stage"], "detail": f["detail"]}, "latency_ms": 0} for f in fs]

    async def record(*, run_id: str, name: str, alias: str, rel: dict[str, Any], bundle: dict[str, Any],
                     p: Principal, user: str, ctx_id: str | None, input: Any, **fields: Any) -> None:
        await runs.create({
            "id": run_id, "agent": name, "alias": alias, "version": rel["version"], "content_hash": rel["content_hash"],
            "mode": "external", "input": input, "trace_id": fields.pop("trace_id", None) or run_id,
            "principal": p.id, "on_behalf_of": user or p.id, "bu": bundle["owner_bu"], "depth": 0,
            "context_id": ctx_id, **fields,
        })

    def wrap(fn: Callable[[Request, Principal], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def handler(request: Request) -> Response:
            try:
                return await fn(request, await caller(request))
            except GovError as e:
                return err(e)

        return outer(handler)

    # ------------------------------------------------------------------ profile

    async def profile(request: Request, p: Principal) -> Response:
        bundle, rel, defn = await load(request, p)
        fctx = context_of(defn, request.query_params.get("context"))
        etag = f'"{rel["content_hash"]}:{fctx.id if fctx else "-"}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        out_schema = defn.contract.output_schema if defn.output.format == "json" else None
        body = {
            "agent": defn.name, "version": rel["version"], "content_hash": rel["content_hash"],
            "alias": request.query_params.get("alias") or settings.default_alias,
            "platform": defn.hosting.platform, "description": defn.description,
            "context": None if fctx is None else {
                "id": fctx.id, "name": fctx.name, "audience": fctx.audience, "role": fctx.role,
                "starters": fctx.starters, "language": fctx.behavior.language},
            "contexts": [{"id": c.id, "name": c.name} for c in defn.contexts],
            # The single prompt to configure in the external agent (system + functional + platform rules).
            "instructions": compose.system_prompt(defn, fctx, rel.get("soft_controls") or [], out_schema),
            "guardrails": [{"type": g["type"], "stage": g["stage"], "action": g.get("action")}
                           for g in [*(rel.get("guardrails") or []), *compose.functional_guardrails(fctx)]],
            "limits": rel["limits"], "model": defn.model.catalog_id,
            "knowledge": compose.knowledge_refs(defn, fctx, rel["version"]),
            "check_api": {"input": f"/gov/v1/agents/{defn.name}/check/input",
                          "output": f"/gov/v1/agents/{defn.name}/check/output",
                          "runs": f"/gov/v1/agents/{defn.name}/runs"},
        }
        return JSONResponse(body, headers={"ETag": etag, "Cache-Control": "private, max-age=30"})

    # ------------------------------------------------------------------ checks

    async def check_input(request: Request, p: Principal) -> Response:
        bundle, rel, defn = await load(request, p)
        b = await request.json()
        fctx = context_of(defn, b.get("context"))
        msg = str(b.get("message") or "")
        enf = enforcer(rel, fctx)
        decision, reply, text, flags = "allow", None, msg, []
        try:
            out = enf.text("input", msg, raise_on_block=False)
            text = out.text
            if out.blocked:
                decision, reply = "block", "Sorry, I can't process that request."
        except PolicyResponse as pr:
            decision, reply = "respond", pr.message
            flags.append("escalated" if pr.kind == "escalation" else f"responded:{pr.kind}")
            enf.findings.append(_finding(pr.kind, pr.detail))
        fs = findings(enf)
        run_id = uuid.uuid4().hex
        finished = decision != "allow"
        await record(run_id=run_id, name=defn.name, alias=request.query_params.get("alias") or settings.default_alias,
                     rel=rel, bundle=bundle, p=p, user=str(b.get("user") or ""), ctx_id=fctx.id if fctx else None,
                     input={"message": text}, trace_id=b.get("session_id"),  # redacted when personal data is
                     status=("ok" if decision == "respond" else "failed") if finished else "running",
                     output=reply, flags=flags, steps=steps(fs),
                     error={"code": "guardrail_blocked", "message": "blocked at input"} if decision == "block" else None,
                     review_status="open" if "escalated" in flags else None)
        return JSONResponse({"check_id": run_id, "decision": decision, "message": reply, "text": text, "flags": flags,
                             "findings": fs, "version": rel["version"], "context": fctx.id if fctx else None,
                             "started_at": time.time()})

    async def check_output(request: Request, p: Principal) -> Response:
        bundle, rel, defn = await load(request, p)
        b = await request.json()
        fctx = context_of(defn, b.get("context"))
        output = b.get("output")
        enf = enforcer(rel, fctx)
        decision = "allow"
        try:
            if isinstance(output, str):
                o = enf.text("output", output, raise_on_block=False)
                output, decision = o.text, "block" if o.blocked else decision
            else:
                output = enf.value("output", output)
            if decision == "allow":
                enf.whole("output", output)
        except (GuardrailBlocked, PolicyResponse):
            decision = "block"
        fs = findings(enf)
        reply = "Sorry, I can't share that answer." if decision == "block" else None
        cid = str(b.get("check_id") or "")
        row = await runs.get(cid) if cid else None
        fields = {"output": reply if decision == "block" else output, "status": "failed" if decision == "block" else "ok",
                  "error": {"code": "guardrail_blocked", "message": "blocked at output"} if decision == "block" else None,
                  "cost_usd": float(b.get("cost_usd") or 0), "input_tokens": int(b.get("input_tokens") or 0),
                  "output_tokens": int(b.get("output_tokens") or 0)}
        if row is not None and row["agent"] == defn.name and row["principal"] == p.id:
            started = _epoch(row["created_at"])
            await runs.update(cid, **fields, steps=[*(row.get("steps") or []), *steps(fs)],
                              latency_ms=int(b.get("latency_ms") or max(0, (time.time() - started) * 1000)),
                              sources=b.get("sources") or [])
        else:
            cid = uuid.uuid4().hex
            await record(run_id=cid, name=defn.name, alias=request.query_params.get("alias") or settings.default_alias,
                         rel=rel, bundle=bundle, p=p, user=str(b.get("user") or ""), ctx_id=fctx.id if fctx else None,
                         input={"message": str(b.get("message") or "")}, steps=steps(fs), flags=[],
                         latency_ms=int(b.get("latency_ms") or 0), **fields)
        return JSONResponse({"check_id": cid, "decision": decision, "output": fields["output"], "message": reply,
                             "findings": fs, "version": rel["version"]})

    async def report_run(request: Request, p: Principal) -> Response:
        """A finished turn reported by the external platform (no inline checks)."""
        bundle, rel, defn = await load(request, p)
        b = await request.json()
        fctx = context_of(defn, b.get("context"))
        status = b.get("status") or "ok"
        if status not in ("ok", "partial", "failed"):
            raise GovError(400, "invalid", "status must be ok, partial or failed")
        flags = [str(f) for f in (b.get("flags") or [])][:20]
        rid = uuid.uuid4().hex
        await record(run_id=rid, name=defn.name, alias=request.query_params.get("alias") or settings.default_alias,
                     rel=rel, bundle=bundle, p=p, user=str(b.get("user") or ""), ctx_id=fctx.id if fctx else None,
                     input={"message": str(b.get("message") or "")}, trace_id=b.get("session_id") or b.get("external_id"),
                     status=status, output=b.get("output"), flags=flags,
                     error={"code": "external_error", "message": str(b.get("error"))[:500]} if b.get("error") else None,
                     cost_usd=float(b.get("cost_usd") or 0), latency_ms=int(b.get("latency_ms") or 0),
                     input_tokens=int(b.get("input_tokens") or 0), output_tokens=int(b.get("output_tokens") or 0),
                     steps=[s for s in (b.get("steps") or []) if isinstance(s, dict)][:200],
                     review_status="open" if "escalated" in flags else None)
        return JSONResponse({"run_id": rid, "recorded": True, "version": rel["version"]}, status_code=201)

    return [
        Route("/gov/v1/agents/{name}/profile", wrap(profile)),
        Route("/gov/v1/agents/{name}/check/input", wrap(check_input), methods=["POST"]),
        Route("/gov/v1/agents/{name}/check/output", wrap(check_output), methods=["POST"]),
        Route("/gov/v1/agents/{name}/runs", wrap(report_run), methods=["POST"]),
    ]


def _finding(kind: str, detail: str) -> Any:
    from .guardrails import Finding

    return Finding(kind, "input", "respond", detail)


def _epoch(iso: str) -> float:
    from datetime import datetime, timezone

    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return time.time()


__all__ = ["build_routes", "now_iso"]
