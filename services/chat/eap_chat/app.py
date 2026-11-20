"""Chat app: a separate front end where people sign in, choose the business unit they act for,
and chat with that BU's agents.

It is deliberately outside the governor.
  * Sign-in: IAP on Cloud Run (Google identity), or - local development only - a simple dev login
    (signed cookie). Dev X-Dev-User headers also work locally.
  * Business unit: the governor's directory says which BUs the person belongs to (any role in a BU).
    The chosen BU is checked on every request; a BU's own variant of a shared agent replaces the core
    agent in that BU's list ("the BU's edition").
  * Calls: the runtime is invoked with the chat app's own service identity plus
    X-EAP-On-Behalf-Of: <user> and X-EAP-User-BU: <bu>; the runtime accepts those only from delegating
    services, and only for chat-enabled agents the BU may use. The person's name, BU and language are
    passed so the agent can adapt its answers. Conversation history is kept in the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from eap_core import gcp
from eap_core.auth import AuthConfig, Authenticator, AuthError

log = logging.getLogger("eap.chat")
HERE = Path(__file__).parent
COOKIE = "eap_chat_dev_user"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class Settings:
    governor_url: str = "http://localhost:8080"
    runtime_url: str = "http://localhost:8081"
    upstream_auth: str = "dev"  # dev | google_id
    auth: AuthConfig = field(default_factory=AuthConfig)
    alias: str = "prod"
    dev_user: str = ""  # local only: user when nobody signed in
    title: str = "Agent Chat"
    session_key: str = ""  # signs the dev login cookie (random per process when empty)

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
            session_key=e.get("EAP_CHAT_SESSION_KEY", ""),
        )


class ChatError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
    dev_login = "dev" in settings.auth.modes
    key = (settings.session_key or secrets.token_hex(32)).encode()

    # ------------------------------------------------------------------ identity

    def sign(value: str) -> str:
        mac = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:32]
        return base64.urlsafe_b64encode(f"{value}|{mac}".encode()).decode()

    def unsign(token: str) -> str | None:
        try:
            value, mac = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 1)
        except Exception:
            return None
        good = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:32]
        return value if hmac.compare_digest(mac, good) else None

    async def upstream_headers(audience: str) -> dict[str, str]:
        if settings.upstream_auth == "google_id":
            return {"Authorization": f"Bearer {await gcp.id_token(audience)}"}
        return {"X-Dev-User": "chat", "X-Dev-Kind": "service"}

    async def user_of(request: Request) -> str:
        try:
            return (await auth.authenticate(request.headers)).id.lower()
        except AuthError:
            if dev_login:
                u = unsign(request.cookies.get(COOKIE, "")) if request.cookies.get(COOKIE) else None
                if u:
                    return u
                if settings.dev_user:
                    return settings.dev_user
            raise

    async def cached(k: str, ttl: float, fn: Any) -> Any:
        hit = cache.get(k)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        v = await fn()
        cache[k] = (time.monotonic(), v)
        return v

    async def profile(user: str, fresh: bool = False) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            r = await gov.get(f"/internal/v1/users/{user}/profile", headers=await upstream_headers(settings.governor_url))
            if r.status_code >= 400:
                raise ChatError(502, f"governor: {r.status_code} {r.text[:200]}")
            return r.json()

        if fresh:
            cache.pop(f"profile:{user}", None)
        return await cached(f"profile:{user}", 60, load)

    async def member_bu(user: str, bu: str | None) -> dict[str, Any]:
        p = await profile(user)
        bus = {b["id"]: b for b in p.get("bus", [])}
        if not bu:
            raise ChatError(400, "choose the business unit you are working for")
        if bu not in bus:
            raise ChatError(403, f"you are not a member of business unit {bu}; ask its BU admin to add you")
        return {**bus[bu], "profile": p}

    async def catalog(bu: str, fresh: bool = False) -> list[dict[str, Any]]:
        async def load() -> list[dict[str, Any]]:
            r = await gov.get("/internal/v1/agents", params={"alias": settings.alias, "channel": "chat", "bu": bu},
                              headers=await upstream_headers(settings.governor_url))
            if r.status_code >= 400:
                raise ChatError(502, f"governor: {r.status_code} {r.text[:200]}")
            return r.json()["agents"]

        if fresh:
            cache.pop(f"agents:{bu}", None)
        return await cached(f"agents:{bu}", 30, load)

    def err(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": {"message": message}}, status_code=status)

    def handler(fn: Any) -> Any:
        async def wrapped(request: Request) -> Response:
            try:
                return await fn(request)
            except AuthError as e:
                return JSONResponse({"error": {"message": str(e)}, "login": "dev" if dev_login else "iap"}, status_code=401)
            except ChatError as e:
                return err(e.status, str(e))

        return wrapped

    # ------------------------------------------------------------------ endpoints

    async def me(request: Request) -> Response:
        user = await user_of(request)
        p = await profile(user, fresh=True)
        return JSONResponse({
            "user": user, "title": settings.title, "display_name": p.get("display_name") or "",
            "default_bu": p.get("default_bu"), "attributes": p.get("attributes") or {},
            "bus": [{"id": b["id"], "name": b["name"], "welcome": (b.get("settings") or {}).get("chat_welcome", "")}
                    for b in p.get("bus", [])],
            "sign_out": "dev" if (dev_login and request.cookies.get(COOKIE)) else ("iap" if "iap" in settings.auth.modes else None),
        })

    async def login(request: Request) -> Response:
        if not dev_login:
            return err(404, "sign-in is handled by Google (IAP)")
        b = await request.json()
        email = str(b.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            return err(400, "enter your email address")
        resp = JSONResponse({"user": email})
        resp.set_cookie(COOKIE, sign(email), httponly=True, samesite="lax", max_age=12 * 3600)
        return resp

    async def logout(request: Request) -> Response:
        resp = JSONResponse({"redirect": "/?gcp-iap-mode=CLEAR_LOGIN_COOKIE" if "iap" in settings.auth.modes else "/"})
        resp.delete_cookie(COOKIE)
        return resp

    async def agents(request: Request) -> Response:
        user = await user_of(request)
        bu = await member_bu(user, request.query_params.get("bu"))
        data = await catalog(bu["id"], fresh=request.query_params.get("refresh") == "1")
        return JSONResponse({"agents": data, "bu": {"id": bu["id"], "name": bu["name"]}})

    async def chat(request: Request) -> Response:
        user = await user_of(request)
        b = await request.json()
        bu = await member_bu(user, b.get("bu"))
        agent, message = str(b.get("agent", "")), str(b.get("message", "")).strip()
        if not agent or not message:
            return err(400, "agent and message are required")
        if agent not in {a["name"] for a in await catalog(bu["id"])}:
            return err(403, f"{agent} is not available for {bu['name']}")
        history = [t for t in (b.get("history") or []) if t.get("role") in ("user", "assistant")][-30:]
        attrs = bu["profile"].get("attributes") or {}
        body = {"input": {"message": message}, "context": b.get("context") or None,
                "history": [{"role": t["role"], "content": str(t.get("content", ""))[:20000]} for t in history],
                "session_id": b.get("session_id") or uuid.uuid4().hex,
                "user": {"name": (bu["profile"].get("display_name") or "")[:100], "bu": bu["id"], "bu_name": bu["name"][:100],
                         "language": str(attrs.get("language") or "")[:20], "job_title": str(attrs.get("job_title") or "")[:100]}}
        headers = {**await upstream_headers(settings.runtime_url), "X-EAP-On-Behalf-Of": user, "X-EAP-User-BU": bu["id"]}
        r = await rt.post(f"/v1/agents/{agent}@{settings.alias}/invoke", json=body, headers=headers)
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:300]
            return err(r.status_code, msg)
        d = r.json()
        out = d.get("output")
        return JSONResponse({
            "reply": out, "status": d.get("status"), "context": d.get("context"), "flags": d.get("flags", []),
            "sources": [{"title": s.get("title"), "uri": s.get("uri")} for s in d.get("sources", [])],
            "run_id": d.get("run_id"), "version": d.get("trace", {}).get("version"),
            "latency_ms": d.get("latency_ms"), "cost_usd": (d.get("cost") or {}).get("usd"),
            "error": (d.get("error") or {}).get("message") if d.get("error") else None,
            "warnings": d.get("warnings", []),
            # orchestration trace, shown as "How this answer was made"
            "steps": [{"kind": s.get("kind"), "name": s.get("name"), "status": s.get("status"),
                       "latency_ms": s.get("latency_ms"), "detail": s.get("detail", {})}
                      for s in (d.get("trace") or {}).get("steps", [])],
        })

    async def feedback(request: Request) -> Response:
        user = await user_of(request)
        b = await request.json()
        run_id = str(b.get("run_id") or "")
        if not run_id or b.get("rating") not in ("up", "down"):
            return err(400, "run_id and rating (up|down) are required")
        headers = {**await upstream_headers(settings.runtime_url), "X-EAP-On-Behalf-Of": user}
        r = await rt.post(f"/v1/runs/{run_id}/feedback", json={"rating": b["rating"],
                                                                "comment": str(b.get("comment") or "")[:2000]},
                          headers=headers)
        if r.status_code >= 400:
            return err(r.status_code, r.text[:200])
        return JSONResponse({"ok": True})

    from eap_core import versioned_index

    index_html, ui_version = versioned_index(HERE / "static")

    async def index(request: Request) -> Response:
        # always revalidate the page; assets are fingerprinted (?v=) so they change with every release
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache", "X-UI-Version": ui_version})

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "chat"})

    return Starlette(routes=[
        Route("/", index),
        Route("/healthz", healthz),
        Route("/api/me", handler(me)),
        Route("/api/login", handler(login), methods=["POST"]),
        Route("/api/logout", handler(logout), methods=["POST"]),
        Route("/api/agents", handler(agents)),
        Route("/api/chat", handler(chat), methods=["POST"]),
        Route("/api/feedback", handler(feedback), methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ])


def main() -> None:  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
