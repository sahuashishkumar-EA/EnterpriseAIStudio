"""Minimal GCP REST helpers using the metadata server for credentials (Cloud Run).

Deliberately small: access tokens, ID tokens, Pub/Sub publish, Cloud Tasks create,
GCS objects, Secret Manager access. Locally (no metadata server) set GCP_ACCESS_TOKEN
or use the local event/task implementations.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import httpx

METADATA = "http://metadata.google.internal/computeMetadata/v1"
_MD_HEADERS = {"Metadata-Flavor": "Google"}

_token_cache: dict[str, tuple[float, str]] = {}


async def access_token(client: httpx.AsyncClient | None = None) -> str:
    if os.environ.get("GCP_ACCESS_TOKEN"):
        return os.environ["GCP_ACCESS_TOKEN"]
    hit = _token_cache.get("access")
    if hit and hit[0] > time.time() + 60:
        return hit[1]
    async with _client(client) as c:
        r = await c.get(f"{METADATA}/instance/service-accounts/default/token", headers=_MD_HEADERS)
        r.raise_for_status()
        data = r.json()
    _token_cache["access"] = (time.time() + data.get("expires_in", 300), data["access_token"])
    return data["access_token"]


async def id_token(audience: str, client: httpx.AsyncClient | None = None) -> str:
    key = f"id:{audience}"
    hit = _token_cache.get(key)
    if hit and hit[0] > time.time() + 60:
        return hit[1]
    async with _client(client) as c:
        r = await c.get(
            f"{METADATA}/instance/service-accounts/default/identity",
            params={"audience": audience, "format": "full"},
            headers=_MD_HEADERS,
        )
        r.raise_for_status()
        token = r.text
    _token_cache[key] = (time.time() + 3000, token)  # Google ID tokens live 1h
    return token


async def project_id(client: httpx.AsyncClient | None = None) -> str:
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return os.environ["GOOGLE_CLOUD_PROJECT"]
    async with _client(client) as c:
        r = await c.get(f"{METADATA}/project/project-id", headers=_MD_HEADERS)
        r.raise_for_status()
        return r.text


class _client:
    """Use the given client or a short-lived one."""

    def __init__(self, client: httpx.AsyncClient | None):
        self._given = client
        self._own: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._given is not None:
            return self._given
        self._own = httpx.AsyncClient(timeout=30)
        return self._own

    async def __aexit__(self, *exc) -> None:
        if self._own is not None:
            await self._own.aclose()


async def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {await access_token()}"}


async def pubsub_publish(topic: str, data: dict[str, Any], attributes: dict[str, str]) -> str:
    """topic = projects/<p>/topics/<t>"""
    body = {
        "messages": [
            {
                "data": base64.b64encode(json.dumps(data).encode()).decode(),
                "attributes": attributes,
            }
        ]
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"https://pubsub.googleapis.com/v1/{topic}:publish", json=body, headers=await _auth_headers())
        r.raise_for_status()
        return r.json()["messageIds"][0]


async def cloud_tasks_create(
    queue: str,
    url: str,
    payload: dict[str, Any],
    *,
    service_account_email: str,
    audience: str | None = None,
    headers: dict[str, str] | None = None,
    schedule_delay_s: int = 0,
    task_id: str | None = None,
) -> str:
    """queue = projects/<p>/locations/<l>/queues/<q>. The task POSTs JSON to `url` with an OIDC token."""
    task: dict[str, Any] = {
        "httpRequest": {
            "httpMethod": "POST",
            "url": url,
            "headers": {"Content-Type": "application/json", **(headers or {})},
            "body": base64.b64encode(json.dumps(payload).encode()).decode(),
            "oidcToken": {"serviceAccountEmail": service_account_email, "audience": audience or url},
        }
    }
    if schedule_delay_s:
        from datetime import datetime, timedelta, timezone

        task["scheduleTime"] = (datetime.now(timezone.utc) + timedelta(seconds=schedule_delay_s)).isoformat()
    if task_id:
        task["name"] = f"{queue}/tasks/{task_id}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://cloudtasks.googleapis.com/v2/{queue}/tasks", json={"task": task}, headers=await _auth_headers()
        )
        if r.status_code == 409:  # duplicate task name => idempotent
            return task.get("name", "")
        r.raise_for_status()
        return r.json()["name"]


async def gcs_upload(bucket: str, name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o",
            params={"uploadType": "media", "name": name},
            content=data,
            headers={**await _auth_headers(), "Content-Type": content_type},
        )
        r.raise_for_status()
    return f"gs://{bucket}/{name}"


async def gcs_download(bucket: str, name: str) -> bytes:
    from urllib.parse import quote

    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(name, safe='')}",
            params={"alt": "media"},
            headers=await _auth_headers(),
        )
        r.raise_for_status()
        return r.content


async def secret_access(name: str) -> str:
    """name = projects/<p>/secrets/<s>/versions/latest"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://secretmanager.googleapis.com/v1/{name}:access", headers=await _auth_headers())
        r.raise_for_status()
        return base64.b64decode(r.json()["payload"]["data"]).decode()
