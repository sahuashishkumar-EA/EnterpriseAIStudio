"""Wires both services in-process: governor <-> runtime over ASGI transports, events from
the governor delivered to the runtime's Pub/Sub push endpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.testclient import TestClient

from eap_core.auth import AuthConfig
from eap_core.db import Database
from eap_core.events import Event, LocalPublisher, push_envelope
from eap_governor.app import create_app as governor_app
from eap_governor.config import Settings as GovSettings
from eap_runtime.app import create_app as runtime_app
from eap_runtime.config import Settings as RtSettings

GOV_SVC = {"X-Dev-User": "governor", "X-Dev-Kind": "service"}


class LazyASGI(httpx.AsyncBaseTransport):
    """ASGI transport whose app is attached after construction (the two apps reference each other)."""

    def __init__(self) -> None:
        self.app = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await httpx.ASGITransport(app=self.app).handle_async_request(request)


@dataclass
class Platform:
    governor: Any
    runtime: Any
    publisher: LocalPublisher
    gov_db: Database
    rt_db: Database

    def gov_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.governor), base_url="http://governor", timeout=60)

    def rt_async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.runtime), base_url="http://runtime", timeout=60)

    def rt_client(self) -> TestClient:
        return TestClient(self.runtime, base_url="http://runtime")

    def gov(self, method: str, path: str, headers: dict[str, str], **kw: Any) -> httpx.Response:
        """Synchronous helper for sync tests (fresh event loop per call)."""

        async def go() -> httpx.Response:
            async with self.gov_client() as c:
                return await c.request(method, path, headers=headers, **kw)

        return asyncio.run(go())


def build(gov_db_url: str = "sqlite://:memory:", rt_db_url: str = "sqlite://:memory:", *,
          gov_overrides: dict[str, Any] | None = None, web_transport: httpx.AsyncBaseTransport | None = None,
          **rt_overrides: Any) -> Platform:
    to_runtime, to_governor = LazyASGI(), LazyASGI()
    sink_client = httpx.AsyncClient(transport=to_runtime, base_url="http://runtime")

    async def push(ev: Event) -> None:
        r = await sink_client.post("/internal/v1/events", json=push_envelope(ev), headers=GOV_SVC)
        r.raise_for_status()

    publisher = LocalPublisher(sinks=[push])
    gov_db, rt_db = Database(gov_db_url), Database(rt_db_url)
    gov = governor_app(GovSettings(database_url=gov_db_url, runtime_url="http://runtime", auth=AuthConfig(modes=["dev"]),
                                   **(gov_overrides or {})),
                       db=gov_db, publisher=publisher, runtime_transport=to_runtime)
    rt_settings = RtSettings(database_url=rt_db_url, governor_url="http://governor", auth=AuthConfig(modes=["dev"]),
                             resolver_ttl_s=rt_overrides.pop("resolver_ttl_s", 300.0), **rt_overrides)
    rt = runtime_app(rt_settings, db=rt_db, governor_transport=to_governor, web_transport=web_transport)
    to_runtime.app, to_governor.app = rt, gov
    return Platform(gov, rt, publisher, gov_db, rt_db)
