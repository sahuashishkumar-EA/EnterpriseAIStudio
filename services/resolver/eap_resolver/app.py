"""C07 resolver HTTP service (Cloud Run, internal ingress, min instances >= 2).

    GET  /resolve?agentId=&businessUnit=&environment=     (spec 7.2)
    POST /internal/v1/events                              Pub/Sub push: config.version.activated
    GET  /healthz
"""

from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from eap_core.auth import AuthConfig, AuthError, Authenticator
from eap_core.events import parse_push
from eap_metamodel.resolve import ResolutionError

from .core import Resolver, Unauthorized, Unavailable


def create_app(resolver: Resolver, authenticator: Authenticator | None = None,
               event_principals: set[str] | None = None) -> Starlette:
    auth = authenticator or Authenticator(AuthConfig.from_env())
    pushers = event_principals if event_principals is not None else {
        p for p in os.environ.get("EAP_EVENT_PUSHERS", "").split(",") if p}

    async def resolve(request: Request):
        q = request.query_params
        missing = [k for k in ("agentId", "businessUnit", "environment") if not q.get(k)]
        if missing:
            return JSONResponse({"error": "bad_request", "message": f"missing {missing}"}, 400)
        try:
            p = await auth.authenticate(request.headers)
            out = await resolver.resolve(p, q["agentId"], q["businessUnit"], q["environment"],
                                         request.headers.get("x-eap-agent-id"))
        except AuthError as e:
            return JSONResponse({"error": "unauthenticated", "message": str(e)}, e.status)
        except Unauthorized as e:
            return JSONResponse({"error": "forbidden", "message": str(e)}, 403)
        except ResolutionError as e:
            return JSONResponse({"error": e.code, "message": str(e)}, 404)
        except Unavailable as e:
            return JSONResponse({"error": "unavailable", "message": str(e)}, 503)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    async def events(request: Request):
        try:
            p = await auth.authenticate(request.headers)
        except AuthError as e:
            return JSONResponse({"error": "unauthenticated", "message": str(e)}, e.status)
        if pushers and p.id not in pushers:
            return JSONResponse({"error": "forbidden"}, 403)
        await resolver.on_event(parse_push(await request.json()))
        return JSONResponse({"ok": True})

    async def healthz(request: Request):
        return JSONResponse({"ok": True, "stats": resolver.stats})

    return Starlette(routes=[
        Route("/resolve", resolve),
        Route("/internal/v1/events", events, methods=["POST"]),
        Route("/healthz", healthz),
    ])


def main() -> None:  # pragma: no cover
    import uvicorn

    from .core import AgentIdentities, FileLkg, HttpSource, MemoryCache, ValkeyCache

    cache = ValkeyCache(os.environ["EAP_VALKEY_URL"]) if os.environ.get("EAP_VALKEY_URL") else MemoryCache()

    async def headers():
        from eap_core import gcp

        return {"Authorization": f"Bearer {await gcp.id_token(os.environ['EAP_REGISTRY_URL'])}"}

    resolver = Resolver(HttpSource(os.environ["EAP_REGISTRY_URL"], headers), cache,
                        FileLkg(os.environ.get("EAP_LKG_DIR", "/tmp/eap-lkg")), AgentIdentities.from_env())
    uvicorn.run(create_app(resolver), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
