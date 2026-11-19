"""C12 Domain MCP server kit: guardrails every domain tool server gets for free.

    server = DomainServer("exposure-mcp", thresholds=ThresholdSource.from_resolver(...), tool_defs=TOOL_DEFS)

    @server.tool("check_exposure", threshold=("amount_eur", "maxExposureEur"))
    async def check_exposure(customer_id: str, amount_eur: float) -> dict: ...

* C12-F2  thresholds are enforced here, server-side, from the active profile/policy values read
          through the resolver (C07). The model's instructions are irrelevant to the outcome.
* C12-F3  the caller's identity (agent, business unit, end user) arrives in request headers set
          by Agent Gateway / the MCP gateway and is propagated to the backend; tools get only it.
* C12-F4  an OpenTelemetry span per call with tool, agent and configuration version attributes.
* C12-F6  input validation against the registered tool definition, per-caller rate limits,
          secrets from Secret Manager (never from configuration).
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import jsonschema
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:
    from opentelemetry import trace

    _tracer = trace.get_tracer("eap.mcp")
except Exception:  # pragma: no cover - otel optional
    _tracer = None


@dataclass(frozen=True)
class Caller:
    agent_id: str
    business_unit: str
    environment: str = "production"
    on_behalf_of: str | None = None  # end user, for least-privilege calls to the backend


_current: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("eap_caller", default=None)


@contextmanager
def caller(c: Caller) -> Iterator[Caller]:
    token = _current.set(c)
    try:
        yield c
    finally:
        _current.reset(token)


def current_caller() -> Caller:
    c = _current.get()
    if c is None:
        raise ToolError("unauthenticated: no calling agent identity")
    return c


class IdentityMiddleware:
    """ASGI middleware: read the identity headers the gateway forwards (C12-F3).

    The gateway authenticates the agent (Agent Identity / workload identity) and sets:
      X-EAP-Agent-Id, X-EAP-Business-Unit, X-EAP-On-Behalf-Of, X-EAP-Environment
    Deploy the server with internal ingress so only the gateway can reach it."""

    def __init__(self, app: Any, environment: str | None = None):
        self.app = app
        self.environment = environment or os.environ.get("EAP_ENVIRONMENT", "production")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if "x-eap-agent-id" not in h:
            return await self.app(scope, receive, send)
        c = Caller(h["x-eap-agent-id"], h.get("x-eap-business-unit", ""), h.get("x-eap-environment", self.environment),
                   h.get("x-eap-on-behalf-of"))
        with caller(c):
            return await self.app(scope, receive, send)


@dataclass
class ResolvedThresholds:
    values: dict[str, float]
    versions: dict[str, str] = field(default_factory=dict)


class ThresholdSource:
    """Active thresholds for the calling agent x BU, cached briefly (a hot change lands within `ttl_s`)."""

    def __init__(self, loader: Callable[[Caller], Any], ttl_s: float = 30.0):
        self.loader = loader
        self.ttl = ttl_s
        self._cache: dict[tuple[str, str, str], tuple[float, ResolvedThresholds]] = {}

    async def get(self, c: Caller) -> ResolvedThresholds:
        k = (c.environment, c.agent_id, c.business_unit)
        hit = self._cache.get(k)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        v = self.loader(c)
        if inspect.isawaitable(v):
            v = await v
        self._cache[k] = (time.monotonic(), v)
        return v

    @staticmethod
    def from_resolver(resolver_url: str, token_provider: Callable[[], str] | None = None,
                      cache_dir: str | None = None) -> "ThresholdSource":
        from eap_agent_config import AgentConfig

        def load_sync(c: Caller) -> ResolvedThresholds:
            cfg = AgentConfig.load(c.agent_id, c.business_unit, c.environment, resolver_url=resolver_url,
                                   token_provider=token_provider, cache_dir=cache_dir,
                                   headers={"X-EAP-Agent-Id": c.agent_id})
            return ResolvedThresholds(cfg.thresholds, cfg.versions)

        async def load(c: Caller) -> ResolvedThresholds:
            import anyio

            return await anyio.to_thread.run_sync(load_sync, c)

        return ThresholdSource(load)


class RateLimiter:
    def __init__(self, per_minute: int):
        self.rate = per_minute / 60.0
        self.capacity = float(per_minute)
        self._b: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, at = self._b.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - at) * self.rate)
        if tokens < 1:
            self._b[key] = (tokens, now)
            return False
        self._b[key] = (tokens - 1, now)
        return True


class DomainServer:
    def __init__(self, name: str, thresholds: ThresholdSource, tool_defs: dict[str, dict[str, Any]],
                 rate_limit_per_minute: int = 120):
        self.name = name
        self.mcp = MCPServer(name)
        self.thresholds = thresholds
        self.tool_defs = tool_defs
        self.limiter = RateLimiter(rate_limit_per_minute)
        self.blocked: list[dict[str, Any]] = []

    def tool(self, name: str, threshold: tuple[str, str] | None = None, description: str | None = None):
        """Register a tool. `threshold=(argument, thresholdKey)` blocks calls whose argument exceeds
        the active threshold value for the calling agent and business unit."""
        tdef = self.tool_defs.get(name)
        if tdef is None:
            raise ValueError(f"tool {name!r} has no registered tool definition (C12-F5)")
        validator = jsonschema.Draft202012Validator(tdef["inputSchema"])

        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(**kwargs):
                c = current_caller()
                if not self.limiter.allow(f"{c.agent_id}/{c.business_unit}"):
                    raise ToolError("rate limit exceeded")
                errors = [e.message for e in validator.iter_errors(kwargs)]
                if errors:
                    raise ToolError("invalid input: " + "; ".join(errors))
                resolved = await self.thresholds.get(c)
                attrs = {"eap.tool": name, "eap.mcp_server": self.name, "eap.agent.id": c.agent_id,
                         "eap.business_unit": c.business_unit,
                         **{f"eap.version.{k}": v for k, v in resolved.versions.items()}}
                span_cm = _tracer.start_as_current_span(f"tool {name}", attributes=attrs) if _tracer else _null()
                with span_cm as span:
                    if threshold:
                        arg, key = threshold
                        limit = resolved.values.get(key)
                        if limit is None:  # fail closed: no active threshold, no call
                            self._block(name, c, key, kwargs.get(arg), None, span)
                            raise ToolError(f"blocked: no active threshold {key!r} for {c.agent_id}/{c.business_unit}")
                        if float(kwargs.get(arg, 0)) > float(limit):
                            self._block(name, c, key, kwargs.get(arg), limit, span)
                            raise ToolError(f"blocked: {arg}={kwargs.get(arg)} exceeds {key}={limit}")
                    return await fn(**kwargs)

            self.mcp.tool(name=name, description=description or tdef.get("description"))(wrapper)
            return fn

        return deco

    def _block(self, tool, c, key, value, limit, span) -> None:
        self.blocked.append({"tool": tool, "agent": c.agent_id, "bu": c.business_unit, "key": key,
                             "value": value, "limit": limit})
        if span is not None and hasattr(span, "set_attribute"):
            span.set_attribute("eap.blocked", True)
            span.set_attribute("eap.threshold.key", key)

    def asgi_app(self):
        """Streamable HTTP app wrapped with identity propagation (deploy on Cloud Run, internal ingress)."""
        return IdentityMiddleware(self.mcp.streamable_http_app())


@contextmanager
def _null():
    yield None


def register_tools(registry_url: str, tool_defs: dict[str, dict[str, Any]], token: str, owner: str) -> list[dict]:
    """C12-F5: register each tool definition in the registry as a tool_def artifact (draft)."""
    import json
    import urllib.request

    out = []
    for name, content in tool_defs.items():
        req = urllib.request.Request(f"{registry_url.rstrip('/')}/artifacts", method="POST",
                                     data=json.dumps({"type": "tool_def", "name": name.replace("_", "-"),
                                                      "owner": owner, "content": content}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            out.append(json.loads(r.read()))
    return out


__all__ = ["Caller", "DomainServer", "IdentityMiddleware", "RateLimiter", "ResolvedThresholds", "ThresholdSource",
           "caller", "current_caller", "register_tools"]
