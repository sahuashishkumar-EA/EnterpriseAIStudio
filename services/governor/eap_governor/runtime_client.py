"""Governor -> Runtime client (evals only). Authenticated with a Google ID token for the
runtime's URL in GCP, or dev service headers locally."""

from __future__ import annotations

from typing import Any

import httpx

from eap_core import gcp


class RuntimeClient:
    def __init__(self, base_url: str, auth: str = "dev", transport: httpx.AsyncBaseTransport | None = None,
                 timeout_s: float = 300):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._client = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=timeout_s)

    async def _headers(self) -> dict[str, str]:
        if self.auth == "google_id":
            return {"Authorization": f"Bearer {await gcp.id_token(self.base_url)}"}
        return {"X-Dev-User": "governor", "X-Dev-Kind": "service"}

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(path, json=payload, headers=await self._headers())
        if r.status_code >= 400:
            raise RuntimeError(f"runtime {path} failed: {r.status_code} {r.text[:500]}")
        return r.json()

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post("/internal/v1/execute", payload)

    async def aclose(self) -> None:
        await self._client.aclose()
