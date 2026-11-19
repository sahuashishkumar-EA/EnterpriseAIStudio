"""In-process Phase 1 platform: every component wired together over ASGI, no network.

Stand-ins (spec 2.3): SQLite for Cloud SQL (Postgres DDL is tested separately), in-process event
bus for Pub/Sub, memory exporter for GCS, local policy mirror for OPA, mock adapter + mock
evaluator for the agent platform, env-var secrets for Secret Manager.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from eap_compiler.adapters import MockAdapter
from eap_compiler.secrets import EnvFileSecrets
from eap_core.auth import AuthConfig, Authenticator, dev_headers
from eap_core.db import Database
from eap_core.events import LocalPublisher
import eap_metamodel
from eap_evalrunner import EvalRunner
from eap_registry import create_app as registry_app
from eap_resolver import MemoryCache, MemoryLkg, Resolver, StoreSource
from eap_resolver.app import create_app as resolver_app
from eap_store import MIGRATIONS_DIR, export, seed, store
from eap_workflow import PromotionEngine, Provisioner

AGENT = "customer-profile"

# named people per role (C05-F3)
PEOPLE = {
    "sally@example.com": [("business_unit", "sales")],
    "bob@example.com": [("bu_owner", "sales")],
    "rob@example.com": [("business_unit", "risk")],
    "rita@example.com": [("risk", "*")],
    "sam@example.com": [("security", "*")],
    "dora@example.com": [("domain_expert", "*")],
    "pat@example.com": [("platform", "*")],
}


async def asgi(app, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """Minimal ASGI client (no httpx needed)."""
    raw = json.dumps(body).encode() if body is not None else b""
    qs = b""
    if "?" in path:
        path, q = path.split("?", 1)
        qs = q.encode()
    h = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body is not None:
        h.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": qs, "headers": h, "server": ("t", 80),
             "client": ("c", 1), "root_path": ""}
    sent = False
    out: dict[str, Any] = {"status": 0, "body": b""}

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await app(scope, receive, send)
    return out["status"], (json.loads(out["body"]) if out["body"] else None)


@dataclass
class Platform:
    loop: asyncio.AbstractEventLoop
    db: Database
    mm: Any
    engine: PromotionEngine
    provisioner: Provisioner
    adapter: MockAdapter
    resolver: Resolver
    registry: Any
    resolver_http: Any
    exporter: export.MemoryExporter
    events: LocalPublisher

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def call(self, method, path, body=None, user="pat@example.com", roles: list[str] | None = None):
        return self.run(asgi(self.registry, method, path, body, dev_headers(user, roles)))

    def resolve(self, bu: str, env: str = "production", agent_identity: str = f"agent:{AGENT}", agent: str = AGENT):
        return self.run(asgi(self.resolver_http, "GET",
                             f"/resolve?agentId={agent}&businessUnit={bu}&environment={env}",
                             headers=dev_headers(agent_identity, kind="service")))

    def find(self, type_: str, name: str) -> dict[str, Any]:
        return self.run(self.db.call(lambda tx: store.find_artifact(tx, type_, name)))

    def latest(self, type_: str, name: str) -> dict[str, Any]:
        a = self.find(type_, name)
        return self.run(self.db.call(lambda tx: store.latest_version(tx, a["id"])))

    def draft(self, type_: str, name: str, mutate, user: str):
        lv = self.latest(type_, name)
        content = json.loads(json.dumps(lv["content"]))
        mutate(content)
        return self.call("POST", f"/artifacts/{lv['artifact_id']}/drafts",
                         {"baseVersionId": lv["id"], "content": content}, user=user)


def build_platform() -> Platform:
    os.environ.setdefault("EAP_SECRET_MOCK_PLATFORM_TOKEN", "local-dev-token")
    loop = asyncio.new_event_loop()
    db = Database("sqlite://:memory:")
    db.migrate(MIGRATIONS_DIR)
    mm = eap_metamodel.load()
    resolver = Resolver(StoreSource(db), MemoryCache(), MemoryLkg())
    events = LocalPublisher(sinks=[resolver.on_event])
    exporter = export.MemoryExporter()
    engine = PromotionEngine(db, mm, EvalRunner(db, mm), events, exporter)
    adapter = MockAdapter(EnvFileSecrets())
    provisioner = Provisioner(engine, adapter)
    auth = Authenticator(AuthConfig(modes=["dev"]))
    reg = registry_app(db, mm, engine, auth, provisioner)
    res_http = resolver_app(resolver, auth, event_principals=set())

    async def boot():
        ids = await seed.seed(db, mm)
        cr = await engine.bootstrap(ids, "production")
        assert cr["state"] == "activated", cr["steps"]

        def people(tx):
            for who, grants in PEOPLE.items():
                for role, scope in grants:
                    store.grant(tx, who, role, scope, "system:test")

        await db.call(people)

    loop.run_until_complete(boot())
    return Platform(loop, db, mm, engine, provisioner, adapter, resolver, reg, res_http, exporter, events)
