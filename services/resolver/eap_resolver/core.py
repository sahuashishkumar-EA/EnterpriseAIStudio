"""C07 Profile resolver: the active, fully resolved configuration per agent x BU x environment.

    cache (Valkey on GCP, memory locally)  ->  registry snapshot  ->  last-known-good

* C07-F1/F2  merge via eap_metamodel.resolve, version IDs of every part
* C07-F3     cache, invalidated by config.version.activated events (Pub/Sub push)
* C07-F4     last-known-good when the registry is unreachable (persisted, not TTL-bound)
* C07-F5     callers authorised by agent identity: an agent reads only its own configuration
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

from eap_core.auth import Principal
from eap_metamodel.resolve import ResolutionError, resolve_config

log = logging.getLogger("eap.resolver")


class Unauthorized(Exception):
    pass


class Unavailable(Exception):
    pass


# ---------------------------------------------------------------------------- caches


class Cache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...
    def set(self, key: str, value: dict[str, Any]) -> None: ...
    def drop_prefix(self, prefix: str) -> int: ...


class MemoryCache:
    def __init__(self, ttl_s: float = 300.0):
        self.ttl = ttl_s
        self._d: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        return None

    def set(self, key, value):
        self._d[key] = (time.monotonic(), value)

    def drop_prefix(self, prefix):
        keys = [k for k in self._d if k.startswith(prefix)]
        for k in keys:
            self._d.pop(k, None)
        return len(keys)


class ValkeyCache:
    """Memorystore for Valkey (Redis protocol). `redis` is imported lazily."""

    def __init__(self, url: str, ttl_s: int = 300, namespace: str = "eap:resolve:"):
        import redis  # type: ignore[import-not-found]

        self.r = redis.Redis.from_url(url, socket_timeout=0.05)
        self.ttl, self.ns = ttl_s, namespace

    def get(self, key):
        raw = self.r.get(self.ns + key)
        return json.loads(raw) if raw else None

    def set(self, key, value):
        self.r.set(self.ns + key, json.dumps(value), ex=self.ttl)

    def drop_prefix(self, prefix):
        n = 0
        for k in self.r.scan_iter(self.ns + prefix + "*"):
            n += self.r.delete(k)
        return n


class MemoryLkg(MemoryCache):
    def __init__(self):
        super().__init__(ttl_s=float("inf"))

    def drop_prefix(self, prefix):  # last-known-good is never dropped by events
        return 0


class FileLkg:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, key):
        return self.dir / (key.replace("/", "_") + ".json")

    def get(self, key):
        p = self._p(key)
        return json.loads(p.read_text()) if p.exists() else None

    def set(self, key, value):
        tmp = self._p(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(self._p(key))

    def drop_prefix(self, prefix):
        return 0


# ---------------------------------------------------------------------------- sources


class Source(Protocol):
    async def snapshot(self, environment: str) -> dict[str, list[dict[str, Any]]]: ...
    async def log_read(self, agent_identity: str, versions: dict[str, str], environment: str, source: str) -> None: ...


class StoreSource:
    """Reads the registry database directly (local stack and tests)."""

    def __init__(self, db: Any):
        self.db = db

    async def snapshot(self, environment):
        from eap_store import store

        return await self.db.call(lambda tx: store.snapshot(tx, environment))

    async def log_read(self, agent_identity, versions, environment, source):
        from eap_store import store

        await self.db.call(lambda tx: store.log_config_read(tx, agent_identity, versions, environment, source))


class HttpSource:
    """Registry API internal endpoints (GCP): GET /internal/v1/snapshot, POST /internal/v1/config-reads."""

    def __init__(self, base_url: str, headers_provider: Any = None, client: Any = None):
        self.base = base_url.rstrip("/")
        self.headers_provider = headers_provider
        self.client = client

    async def _headers(self):
        return await self.headers_provider() if self.headers_provider else {}

    async def snapshot(self, environment):
        import httpx

        c = self.client or httpx.AsyncClient(timeout=2.0)
        r = await c.get(f"{self.base}/internal/v1/snapshot", params={"environment": environment},
                        headers=await self._headers())
        r.raise_for_status()
        return r.json()["snapshot"]

    async def log_read(self, agent_identity, versions, environment, source):
        import httpx

        c = self.client or httpx.AsyncClient(timeout=2.0)
        await c.post(f"{self.base}/internal/v1/config-reads", headers=await self._headers(), json={
            "agentIdentity": agent_identity, "versions": versions, "environment": environment, "source": source})


# ---------------------------------------------------------------------------- identity (C07-F5)


class AgentIdentities:
    """Maps an authenticated principal to the agent id it may read.

    * Agent Identity / OIDC token claim `eap_agent_id`
    * EAP_AGENT_IDENTITIES="customer-profile=sa@p.iam.gserviceaccount.com,..." for service accounts
    * dev mode: X-Dev-User "agent:customer-profile"
    * trusted platform services (config MCP server, domain MCP servers) act for the agent they
      authenticated and pass it in X-EAP-Agent-Id: EAP_RESOLVER_TRUSTED="config-mcp@...,..."
    """

    def __init__(self, mapping: dict[str, str] | None = None, trusted: set[str] | None = None):
        self.mapping = mapping or {}
        self.trusted = trusted or set()

    @staticmethod
    def from_env() -> "AgentIdentities":
        m = {}
        for item in filter(None, os.environ.get("EAP_AGENT_IDENTITIES", "").split(",")):
            agent, _, principal = item.partition("=")
            m[principal.strip()] = agent.strip()
        trusted = {t.strip() for t in os.environ.get("EAP_RESOLVER_TRUSTED", "").split(",") if t.strip()}
        return AgentIdentities(m, trusted)

    def agent_for(self, p: Principal, on_behalf_of: str | None = None) -> str:
        if p.id in self.trusted:
            if not on_behalf_of:
                raise Unauthorized("trusted services must name the agent they act for (X-EAP-Agent-Id)")
            return on_behalf_of
        if p.claims.get("eap_agent_id"):
            return p.claims["eap_agent_id"]
        if p.id in self.mapping:
            return self.mapping[p.id]
        if p.mode == "dev" and p.sub.startswith("agent:"):
            return p.sub.split(":", 1)[1]
        raise Unauthorized(f"{p.id} is not an agent identity")


# ---------------------------------------------------------------------------- resolver


class Resolver:
    def __init__(self, source: Source, cache: Cache | None = None, lkg: Cache | None = None,
                 identities: AgentIdentities | None = None):
        self.source = source
        self.cache = cache or MemoryCache()
        self.lkg = lkg or MemoryLkg()
        self.identities = identities or AgentIdentities()
        self.stats = {"cache": 0, "registry": 0, "last-known-good": 0}

    @staticmethod
    def key(environment: str, agent_id: str, bu: str) -> str:
        return f"{environment}/{agent_id}/{bu}"

    async def resolve(self, principal: Principal, agent_id: str, business_unit: str, environment: str,
                      on_behalf_of: str | None = None) -> dict[str, Any]:
        caller_agent = self.identities.agent_for(principal, on_behalf_of)
        if caller_agent != agent_id:
            raise Unauthorized(f"{principal.id} may read only {caller_agent}")
        k = self.key(environment, agent_id, business_unit)
        hit = self.cache.get(k)
        if hit is not None:
            out = {**hit, "source": "cache"}
        else:
            try:
                snap = await self.source.snapshot(environment)
                resolved = resolve_config(snap, agent_id, business_unit)
                resolved["resolvedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.cache.set(k, resolved)
                self.lkg.set(k, resolved)
                out = {**resolved, "source": "registry"}
            except ResolutionError:
                raise
            except Exception as e:  # registry unreachable -> C07-F4
                lkg = self.lkg.get(k)
                if lkg is None:
                    raise Unavailable(f"registry unavailable and no last-known-good for {k}: {e}") from e
                log.warning("serving last-known-good for %s: %s", k, e)
                out = {**lkg, "source": "last-known-good"}
        self.stats[out["source"]] += 1
        try:
            await self.source.log_read(principal.id, out["versions"], environment, out["source"])
        except Exception:  # audit of reads must not take the runtime path down; Cloud Logging has a copy
            log.warning("config read log unavailable", exc_info=True)
        return out

    async def on_event(self, event: Any) -> None:
        """C07-F3: drop cached entries for the environment that changed."""
        if getattr(event, "event_type", "") not in ("config.version.activated", "config.provisioning.completed"):
            return
        env = event.data.get("environment")
        if env:
            self.cache.drop_prefix(f"{env}/")
