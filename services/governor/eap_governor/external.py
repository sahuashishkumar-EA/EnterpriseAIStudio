"""Agents that run outside the platform (hosting.platform != "internal").

- Eval targets: the release gate tests the *real* external agent before every go-live, through its
  endpoint (plain HTTP, or Microsoft Copilot Studio via Bot Framework Direct Line 3.0).
- Change webhooks: when a version goes live, the external platform is told (HMAC-SHA256 signed) so it
  can re-read its profile from /gov/v1/agents/{name}/profile.

Secrets are referenced by name (hosting.auth_secret / webhook_secret) and read from the environment;
on Cloud Run, mount the Secret Manager secret as an environment variable of that name.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("eap.governor.external")

DIRECT_LINE = "https://directline.botframework.com/v3/directline"
POLL_S, QUIET_S = 1.0, 2.0  # Direct Line polling interval / silence that ends a bot reply


class ExternalError(Exception):
    pass


def secret(name: str) -> str:
    if not name:
        return ""
    v = os.environ.get(name)
    if v is None:
        raise ExternalError(f"secret {name} is not set on the governor (environment variable / Secret Manager mount)")
    return v


def _message(inp: Any) -> str:
    if isinstance(inp, dict):
        for k in ("message", "question", "query", "text"):
            if isinstance(inp.get(k), str):
                return inp[k]
        return json.dumps(inp)
    return str(inp)


def _result(output: Any, *, status: str = "ok", flags: list[str] | None = None, started: float, error: str = "",
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": status, "output": output, "flags": flags or [], "latency_ms": int((time.time() - started) * 1000),
            "cost": {"usd": 0.0}, "error": {"code": "external_error", "message": error} if error else None,
            "trace": {"steps": [{"kind": "info", "name": "external", "status": status, "detail": extra or {}}]}}


class HttpTarget:
    """POST {endpoint} {"message", "input", "context", "history"} -> {"output" | "reply" | "text", "flags"?}."""

    def __init__(self, hosting: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 120):
        self.url = hosting["endpoint"]
        self.token = secret(hosting.get("auth_secret", ""))
        self.client = httpx.AsyncClient(transport=transport, timeout=timeout_s)

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = await self.client.post(self.url, headers=headers, json={
                "message": _message(payload.get("input")), "input": payload.get("input"),
                "context": payload.get("context"), "history": payload.get("history") or []})
        except httpx.HTTPError as e:
            return _result(None, status="failed", started=t0, error=f"external agent unreachable: {e}")
        if r.status_code >= 400:
            return _result(None, status="failed", started=t0, error=f"external agent returned {r.status_code}: {r.text[:300]}")
        try:
            j = r.json()
        except ValueError:
            return _result(r.text, started=t0, extra={"endpoint": self.url})
        out = j.get("output", j.get("reply", j.get("text"))) if isinstance(j, dict) else j
        return _result(out, status=(j.get("status") or "ok") if isinstance(j, dict) else "ok",
                       flags=list(j.get("flags") or []) if isinstance(j, dict) else [], started=t0, extra={"endpoint": self.url})


class DirectLineTarget:
    """Microsoft Copilot Studio (or any Bot Framework bot) through Direct Line 3.0.

    One conversation per test case: start a conversation, send the message (functional context in
    channelData.eap_context), then poll activities until the bot's replies to that message stop arriving.
    """

    def __init__(self, hosting: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = 45, quiet_s: float | None = None, poll_s: float | None = None):
        self.base = (hosting.get("endpoint") or DIRECT_LINE).rstrip("/")
        self.secret = secret(hosting.get("auth_secret", ""))
        if not self.secret:
            raise ExternalError("Copilot Studio testing needs the Direct Line secret: set hosting.auth_secret")
        self.client = httpx.AsyncClient(transport=transport, timeout=30)
        self.timeout_s = timeout_s
        self.quiet_s = QUIET_S if quiet_s is None else quiet_s
        self.poll_s = POLL_S if poll_s is None else poll_s

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        auth = {"Authorization": f"Bearer {self.secret}"}
        try:
            c = await self.client.post(f"{self.base}/conversations", headers=auth)
            c.raise_for_status()
            conv = c.json()
            cid, token = conv["conversationId"], conv.get("token") or self.secret
            auth = {"Authorization": f"Bearer {token}"}
            sent = await self.client.post(f"{self.base}/conversations/{cid}/activities", headers=auth, json={
                "type": "message", "from": {"id": "eap-eval", "name": "EAP test"}, "text": _message(payload.get("input")),
                "channelData": {"eap_context": payload.get("context")}})
            sent.raise_for_status()
            act_id = sent.json().get("id")
            replies: list[str] = []
            seen: set[str] = set()
            watermark, last_reply = None, None
            while time.time() - t0 < self.timeout_s:
                r = await self.client.get(f"{self.base}/conversations/{cid}/activities", headers=auth,
                                          params={"watermark": watermark} if watermark else None)
                r.raise_for_status()
                j = r.json()
                watermark = j.get("watermark", watermark)
                for a in j.get("activities", []):
                    if a.get("type") == "message" and (a.get("from") or {}).get("id") != "eap-eval" \
                            and (a.get("replyToId") in (None, act_id)) and a.get("text"):
                        key = a.get("id") or a["text"]
                        if key in seen:  # servers that ignore the watermark resend activities
                            continue
                        seen.add(key)
                        replies.append(a["text"])
                        last_reply = time.time()
                if last_reply and time.time() - last_reply >= self.quiet_s:
                    break
                await asyncio.sleep(self.poll_s)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return _result(None, status="failed", started=t0, error=f"Direct Line: {e}")
        if not replies:
            return _result(None, status="failed", started=t0, error=f"no reply from the bot within {self.timeout_s:.0f}s")
        return _result("\n\n".join(replies), started=t0, extra={"conversation": cid, "replies": len(replies)})


def target_for(definition: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> Any | None:
    """The eval target for a definition, or None to execute on the platform runtime."""
    h = definition.get("hosting") or {}
    platform = h.get("platform", "internal")
    if platform == "internal":
        return None
    if platform == "copilot_studio":
        return DirectLineTarget(h, transport)
    if platform in ("http", "a2a"):
        if not h.get("endpoint"):
            raise ExternalError(f"hosting.endpoint is required to test a {platform} agent")
        return HttpTarget(h, transport)
    raise ExternalError(f"unknown hosting platform {platform}")


# ---------------------------------------------------------------- webhooks

def sign(secret_value: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret_value.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify(secret_value: str, timestamp: str, body: bytes, signature: str, max_age_s: int = 300) -> bool:
    """Receiver side (documented for external platforms): check signature and freshness."""
    try:
        fresh = abs(time.time() - int(timestamp)) <= max_age_s
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sign(secret_value, timestamp, body), signature)


async def notify(hosting: dict[str, Any], event: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> int | None:
    url = hosting.get("webhook_url")
    if not url:
        return None
    body = json.dumps(event, sort_keys=True).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-EAP-Timestamp": ts, "X-EAP-Event": event.get("event", "")}
    if hosting.get("webhook_secret"):
        headers["X-EAP-Signature"] = sign(secret(hosting["webhook_secret"]), ts, body)
    async with httpx.AsyncClient(transport=transport, timeout=10) as c:
        r = await c.post(url, content=body, headers=headers)
    if r.status_code >= 400:
        log.warning("webhook %s returned %s", url, r.status_code)
    return r.status_code
