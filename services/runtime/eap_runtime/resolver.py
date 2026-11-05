"""Resolver client: pulls published, immutable definitions + effective policy from the
Governor API (never its tables).

  * short TTL cache per (agent, alias)
  * conditional revalidation with If-None-Match / ETag (304 = keep cached bundle)
  * Pub/Sub `alias.moved` (and related) events invalidate entries immediately
  * stale-if-error: if the Governor is unreachable, serve the last bundle up to max_stale
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import httpx

from eap_core import gcp

log = logging.getLogger("eap.resolver")


class ResolveError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class Resolver:
    def __init__(
        self,
        base_url: str,
        auth: str = "dev",
        transport: httpx.AsyncBaseTransport | None = None,
        ttl_s: float = 30.0,
        max_stale_s: float = 600.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.ttl_s = ttl_s
        self.max_stale_s = max_stale_s
        self._client = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=15)
        self._cache: dict[tuple[str, str], tuple[float, str, dict[str, Any]]] = {}
        self.stats = {"hits": 0, "revalidated": 0, "fetched": 0, "stale_served": 0, "invalidations": 0}

    async def _headers(self) -> dict[str, str]:
        if self.auth == "google_id":
            return {"Authorization": f"Bearer {await gcp.id_token(self.base_url)}"}
        return {"X-Dev-User": "runtime", "X-Dev-Kind": "service"}

    async def resolve(self, agent: str, alias: str) -> dict[str, Any]:
        key = (agent, alias)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl_s:
            self.stats["hits"] += 1
            return hit[2]
        headers = await self._headers()
        if hit:
            headers["If-None-Match"] = hit[1]
        try:
            r = await self._client.get(f"/internal/v1/resolve/{agent}/{alias}", headers=headers)
        except httpx.HTTPError as e:
            if hit and now - hit[0] < self.max_stale_s:
                self.stats["stale_served"] += 1
                log.warning("governor unreachable, serving stale bundle for %s@%s: %s", agent, alias, e)
                return hit[2]
            raise ResolveError(f"governor unreachable: {e}") from e
        if r.status_code == 304 and hit:
            self.stats["revalidated"] += 1
            self._cache[key] = (now, hit[1], hit[2])
            return hit[2]
        if r.status_code == 404:
            self._cache.pop(key, None)
            raise ResolveError(f"agent {agent}@{alias} not found", 404)
        if r.status_code >= 400:
            if hit and r.status_code >= 500 and now - hit[0] < self.max_stale_s:
                self.stats["stale_served"] += 1
                return hit[2]
            raise ResolveError(f"resolve failed: {r.status_code} {r.text[:300]}", 502)
        bundle = r.json()
        self.stats["fetched"] += 1
        self._cache[key] = (now, r.headers.get("etag", bundle.get("etag", "")), bundle)
        return bundle

    def invalidate(self, agent: str | None = None) -> int:
        keys = [k for k in self._cache if agent is None or k[0] == agent]
        for k in keys:
            # keep the bundle (for stale-if-error) but force revalidation
            ts, etag, b = self._cache[k]
            self._cache[k] = (-1e12, etag, b)
        self.stats["invalidations"] += 1
        return len(keys)

    async def list_exposed(self, alias: str) -> list[dict[str, Any]]:
        r = await self._client.get("/internal/v1/agents", params={"alias": alias}, headers=await self._headers())
        r.raise_for_status()
        return r.json()["agents"]

    async def aclose(self) -> None:
        await self._client.aclose()


def pick_release(bundle: dict[str, Any], routing_key: str) -> dict[str, Any]:
    """Deterministic canary split: the same routing key (session / trace) always gets the
    same version."""
    canary = bundle.get("canary")
    weight = int(bundle.get("canary_weight") or 0)
    if canary and weight > 0:
        bucket = int(hashlib.sha256(f"{bundle['agent']}:{routing_key}".encode()).hexdigest()[:8], 16) % 100
        if bucket < weight:
            return canary
    return bundle["primary"]
