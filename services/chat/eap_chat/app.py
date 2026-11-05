"""Demo chat app: a separate front end that lets signed-in users chat with published agents.

It is deliberately outside the governor. It authenticates the end user (IAP on Cloud Run,
or dev headers locally), lists chat-enabled agents and their functional contexts from the
governor's internal API, and invokes the runtime with its own service identity plus
X-EAP-On-Behalf-Of: <user>. The runtime accepts that header only from delegating services and
only for agents with exposure.chat = true. Conversation history is kept in the browser.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from eap_core import gcp
from eap_core.auth import AuthConfig, Authenticator, AuthError

log = logging.getLogger("eap.chat")
HERE = Path(__file__).parent


@dataclass
class Settings:
    governor_url: str = "http://localhost:8080"
    runtime_url: str = "http://localhost:8081"
    upstream_auth: str = "dev"  # dev | google_id
    auth: AuthConfig = field(default_factory=AuthConfig)
    alias: str = "prod"
    dev_user: str = ""  # local only: user when no auth headers are present
    title: str = "Agent Chat"

    @staticmethod
    def from_env() -> "Settings":
        e = os.environ
        return Settings(
            governor_url=e.get("EAP_GOVERNOR_URL", "http://localhost:8080"),
            runtime_url=e.get("EAP_RUNTIME_URL", "http://localhost:8081"),
            upstream_auth=e.get("EAP_UPSTREAM_AUTH", "dev"),
            auth=AuthConfig.from_env(),
            alias=e.get("EAP_CHAT_ALIAS", "prod"),
            dev_user=e.get("EAP_CHAT_DEV_USER", ""),
            title=e.get("EAP_CHAT_TITLE", "Agent Chat"),
        )


def create_app(
    settings: Settings | None = None,
    *,
    governor_transport: httpx.AsyncBaseTransport | None = None,
    runtime_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    auth = Authenticator(settings.auth)
    gov = httpx.AsyncClient(base_url=settings.governor_url, transport=governor_transport, timeout=30)
    rt = httpx.AsyncClient(base_url=settings.runtime_url, transport=runtime_transport, timeout=300)
    cache: dict[str, tuple[float, Any]] = {}

    async def upstream_headers(audience: str) -> dict[str, str]:
        if settings.upstream_auth == "google_id":
            return {"Authorization": f"Bearer {await gcp.id_token(audience)}"}
        return {"X-Dev-User": "chat", "X-Dev-Kind": "service"}

    async def user_of(request: Request) -> str:
        try:
            return (await auth.authenticate(request.headers)).id
        except AuthError:
            if settings.dev_user and "dev" in settings.auth.modes:
                return settings.dev_user
            raise

    def err(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": {"message": message}}, status_code=status)

    async def me(request: Request) -> Response:
        try:
            return JSONResponse({"user": await user_of(request), "title": settings.title})
        except AuthError as e:
            return err(401, str(e))

    async def agents(request: Request) -> Response:
        try:
            await user_of(request)
        except AuthError as e:
            return err(401, str(e))
        hit = cache.get("agents")
        if hit and time.monotonic() - hit[0] < 30 and request.query_params.get("refresh") != "1":
            return JSONResponse({"agents": hit[1]})
        r = await gov.get("/internal/v1/agents", params={"alias": settings.alias, "channel": "chat"},
                          headers=await upstream_headers(settings.governor_url))
        if r.status_code >= 400:
            return err(502, f"governor: {r.status_code} {r.text[:200]}")
        data = r.json()["agents"]
        cache["agents"] = (time.monotonic(), data)
        return JSONResponse({"agents": data})

    async def chat(request: Request) -> Response:
        try:
            user = await user_of(request)
        except AuthError as e:
            return err(401, str(e))
        b = await request.json()
        agent, message = str(b.get("agent", "")), str(b.get("message", "")).strip()
        if not agent or not message:
            return err(400, "agent and message are required")
        history = [t for t in (b.get("history") or []) if t.get("role") in ("user", "assistant")][-30:]
        body = {"input": {"message": message}, "context": b.get("context") or None,
                "history": [{"role": t["role"], "content": str(t.get("content", ""))[:20000]} for t in history],
                "session_id": b.get("session_id") or uuid.uuid4().hex}
        headers = {**await upstream_headers(settings.runtime_url), "X-EAP-On-Behalf-Of": user}
        r = await rt.post(f"/v1/agents/{agent}@{settings.alias}/invoke", json=body, headers=headers)
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:300]
            return err(r.status_code, msg)
        d = r.json()
        out = d.get("output")
        reply = out if isinstance(out, str) else (None if out is None else out)
        return JSONResponse({
            "reply": reply, "status": d.get("status"), "context": d.get("context"), "flags": d.get("flags", []),
            "sources": [{"title": s.get("title"), "uri": s.get("uri")} for s in d.get("sources", [])],
            "run_id": d.get("run_id"), "version": d.get("trace", {}).get("version"),
            "latency_ms": d.get("latency_ms"), "cost_usd": (d.get("cost") or {}).get("usd"),
            "error": (d.get("error") or {}).get("message") if d.get("error") else None,
            "warnings": d.get("warnings", []),
        })

    async def index(request: Request) -> Response:
        return FileResponse(HERE / "static" / "index.html")

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "chat"})

    return Starlette(routes=[
        Route("/", index),
        Route("/healthz", healthz),
        Route("/api/me", me),
        Route("/api/agents", agents),
        Route("/api/chat", chat, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ])


def main() -> None:  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
