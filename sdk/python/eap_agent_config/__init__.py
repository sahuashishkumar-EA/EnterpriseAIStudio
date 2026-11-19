"""C09 Agent config SDK (Python) for pro-code agents.

    cfg = AgentConfig.load(agent_id="customer-profile", business_unit="sales", environment="production")
    cfg.profile["persona"]; cfg.thresholds; cfg.versions      # pinned for the session (C09-F1)
    span.set_attributes(cfg.otel_attributes())                # C09-F2

* Fetches the resolved configuration from the profile resolver (C07) once, at session start,
  and pins it: later activations reach the *next* session, never the current one.
* Keeps a local last-known-good copy and uses it when the resolver is unreachable (C09-F4).
* No third-party dependencies: urllib from the standard library; pass `transport` to inject one.

On Agent Runtime the agent's own identity token is sent (Agent Identity); elsewhere pass a
`token_provider` returning an ID token for the resolver's audience.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .generated import SCHEMA_VERSIONS  # noqa: F401  (typed access, C09-F3)

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, Any]]]


class ConfigUnavailable(Exception):
    pass


def _urllib_transport(timeout_s: float) -> Transport:
    def call(url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    return call


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    business_unit: str
    environment: str
    raw: dict[str, Any] = field(repr=False)
    source: str = "registry"
    loaded_at: float = 0.0

    # ------------------------------------------------------------------ typed accessors

    @property
    def agent(self) -> dict[str, Any]:
        return self.raw.get("agent", {})

    @property
    def profile(self) -> dict[str, Any]:
        return self.raw.get("profile", {})

    @property
    def persona(self) -> dict[str, Any]:
        return self.profile.get("persona", {})

    @property
    def bindings(self) -> dict[str, Any]:
        return self.raw.get("bindings", {})

    @property
    def thresholds(self) -> dict[str, float]:
        return self.raw.get("thresholds", {}).get("values", {})

    @property
    def versions(self) -> dict[str, str]:
        return dict(self.raw.get("versions", {}))

    def otel_attributes(self) -> dict[str, str]:
        """Attributes for every span and outcome event (C09-F2)."""
        out = {"eap.agent.id": self.agent_id, "eap.business_unit": self.business_unit,
               "eap.environment": self.environment, "eap.config.source": self.source}
        for k, v in self.versions.items():
            out[f"eap.version.{k}"] = v
        return out

    # ------------------------------------------------------------------ loading

    @staticmethod
    def _cache_file(cache_dir: str | None, agent_id: str, bu: str, env: str) -> Path | None:
        d = cache_dir or os.environ.get("EAP_CONFIG_CACHE_DIR")
        if not d:
            return None
        return Path(d) / f"{env}__{agent_id}__{bu}.json"

    @classmethod
    def load(cls, agent_id: str, business_unit: str, environment: str, resolver_url: str | None = None,
             token_provider: Callable[[], str] | None = None, transport: Transport | None = None,
             cache_dir: str | None = None, timeout_s: float = 2.0, headers: dict[str, str] | None = None) -> "AgentConfig":
        base = (resolver_url or os.environ.get("EAP_RESOLVER_URL", "")).rstrip("/")
        call = transport or _urllib_transport(timeout_s)
        qs = urllib.parse.urlencode({"agentId": agent_id, "businessUnit": business_unit, "environment": environment})
        h = dict(headers or {})
        if token_provider:
            h["Authorization"] = f"Bearer {token_provider()}"
        cache = cls._cache_file(cache_dir, agent_id, business_unit, environment)
        try:
            status, data = call(f"{base}/resolve?{qs}", h)
            if status != 200:
                raise ConfigUnavailable(f"resolver answered {status}: {data.get('message')}")
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data))
            return cls(agent_id, business_unit, environment, data, data.get("source", "registry"), time.time())
        except (ConfigUnavailable, OSError) as e:
            if status_is_auth(e):
                raise
            if cache and cache.exists():  # C09-F4
                data = json.loads(cache.read_text())
                return cls(agent_id, business_unit, environment, data, "sdk-last-known-good", time.time())
            raise ConfigUnavailable(f"no configuration for {agent_id}/{business_unit}/{environment}: {e}") from e


def status_is_auth(e: Exception) -> bool:
    """403/401 are not outages: never fall back to a cached copy for a caller that is not allowed."""
    return isinstance(e, ConfigUnavailable) and (" 401" in str(e) or " 403" in str(e))


__all__ = ["AgentConfig", "ConfigUnavailable"]
