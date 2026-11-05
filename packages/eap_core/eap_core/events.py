"""Event contract. All platform events go to ONE Pub/Sub topic; the `event_type`
attribute distinguishes them (subscriptions may filter on it).

  alias.moved            {agent, alias, version, canary_version, canary_weight}
  version.published      {agent, version, content_hash}
  version.flagged        {agent, version, reason}
  model.status_changed   {model, status, flagged_versions}
  policy.updated         {scope}
  catalog.updated        {kind, id}

The runtime consumes them via a push subscription on /internal/v1/events to invalidate its
resolver cache, so behaviour changes without a redeploy.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

import httpx
from pydantic import BaseModel, Field

from .db import now_iso

log = logging.getLogger("eap.events")

EVENT_TYPES = (
    "alias.moved",
    "version.published",
    "version.flagged",
    "model.status_changed",
    "policy.updated",
    "catalog.updated",
)


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str
    occurred_at: str = Field(default_factory=now_iso)
    source: str = "governor"
    data: dict[str, Any] = Field(default_factory=dict)

    def attributes(self) -> dict[str, str]:
        attrs = {"event_type": self.event_type, "event_id": self.event_id, "source": self.source}
        if "agent" in self.data:
            attrs["agent"] = str(self.data["agent"])
        return attrs


def push_envelope(event: Event, subscription: str = "local") -> dict[str, Any]:
    """Pub/Sub push request body for `event` (same shape the real push subscription sends)."""
    return {
        "message": {
            "data": base64.b64encode(event.model_dump_json().encode()).decode(),
            "attributes": event.attributes(),
            "messageId": event.event_id,
            "publishTime": event.occurred_at,
        },
        "subscription": subscription,
    }


def parse_push(body: dict[str, Any]) -> Event:
    msg = body.get("message") or {}
    raw = base64.b64decode(msg.get("data", "") or b"e30=")  # "{}"
    data = json.loads(raw or b"{}")
    if "event_type" not in data:
        data["event_type"] = (msg.get("attributes") or {}).get("event_type", "unknown")
    return Event.model_validate(data)


class Publisher:
    async def publish(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class PubSubPublisher(Publisher):
    def __init__(self, topic: str):
        self.topic = topic  # projects/<p>/topics/<t>

    async def publish(self, event: Event) -> None:
        from . import gcp

        await gcp.pubsub_publish(self.topic, event.model_dump(mode="json"), event.attributes())


Sink = Callable[[Event], Awaitable[None]]


class LocalPublisher(Publisher):
    """In-process sinks and/or HTTP push targets (docker-compose: governor -> runtime)."""

    def __init__(
        self,
        sinks: list[Sink] | None = None,
        push_urls: list[str] | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.sinks = list(sinks or [])
        self.push_urls = list(push_urls or [])
        self.headers = headers or {}
        self.client = client
        self.sent: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.sent.append(event)
        for sink in self.sinks:
            try:
                await sink(event)
            except Exception:  # events are best-effort; TTL bounds staleness anyway
                log.exception("event sink failed")
        for url in self.push_urls:
            try:
                if self.client is not None:
                    await self.client.post(url, json=push_envelope(event), headers=self.headers)
                else:
                    async with httpx.AsyncClient(timeout=10) as c:
                        await c.post(url, json=push_envelope(event), headers=self.headers)
            except Exception:
                log.exception("event push to %s failed", url)


def publisher_from_env() -> Publisher:
    import os

    topic = os.environ.get("EAP_EVENTS_TOPIC")
    if topic:
        return PubSubPublisher(topic)
    urls = [u for u in os.environ.get("EAP_EVENTS_PUSH_URLS", "").split(",") if u]
    headers = {}
    if os.environ.get("EAP_EVENTS_PUSH_DEV_USER"):
        headers = {"X-Dev-User": os.environ["EAP_EVENTS_PUSH_DEV_USER"], "X-Dev-Kind": "service"}
    return LocalPublisher(push_urls=urls, headers=headers)
