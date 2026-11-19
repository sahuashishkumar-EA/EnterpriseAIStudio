"""Policy step of the promotion workflow (C05-F1 step 2).

OpaPolicy calls the OPA REST API (an internal Cloud Run service on GCP). LocalPolicy mirrors the
Rego rules in /policies/eap/registry.rego so unit tests run without the OPA binary (ADR-006);
CI should run `opa test policies/` and a parity test (backlog) to keep the two in step.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

IBAN_LIKE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}")
EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class PolicyEngine(Protocol):
    async def deny(self, policy_input: dict[str, Any]) -> list[str]: ...


def _free_text(content: dict[str, Any]) -> str:
    return " ".join([content.get("instructions", "")] + [e.get("input", "") for e in content.get("examples", [])])


class LocalPolicy:
    name = "local-mirror"

    async def deny(self, policy_input: dict[str, Any]) -> list[str]:
        out = []
        env = policy_input.get("environment")
        for v in policy_input.get("versions", []):
            t, n, c = v["type"], v["name"], v["content"]
            if t in ("profile", "skill"):
                text = _free_text(c)
                if IBAN_LIKE.search(text):
                    out.append(f"{t} {n}: instructions contain what looks like an IBAN")
                if EMAIL_LIKE.search(text):
                    out.append(f"{t} {n}: instructions contain an e-mail address; use escalationContacts")
            if t == "agent" and c.get("riskTier") == "high" and not c.get("identityRef"):
                out.append(f"agent {n}: high-risk agents need identityRef")
            if env == "production" and t == "tool_def" and not str(c.get("endpoint", "https://")).startswith("https://"):
                out.append(f"tool_def {n}: endpoint must use https in production")
        return sorted(out)


class OpaPolicy:
    def __init__(self, base_url: str, package_path: str = "eap/registry", client: Any = None):
        self.base_url = base_url.rstrip("/")
        self.path = package_path
        self.client = client
        self.name = f"opa:{self.base_url}"

    async def deny(self, policy_input: dict[str, Any]) -> list[str]:
        import httpx

        url = f"{self.base_url}/v1/data/{self.path}/deny"
        if self.client is not None:
            r = await self.client.post(url, json={"input": policy_input})
        else:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json={"input": policy_input})
        r.raise_for_status()
        return sorted(r.json().get("result", []))


def policy_from_env() -> PolicyEngine:
    import os

    url = os.environ.get("EAP_OPA_URL")
    return OpaPolicy(url) if url else LocalPolicy()
