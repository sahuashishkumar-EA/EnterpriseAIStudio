"""Live web search for functional contexts (context.web_search).

Backends (EAP_WEB_SEARCH):
  gemini  - Gemini Developer API with the Google Search tool (GOOGLE_AI_API_KEY)
  vertex  - Vertex AI Gemini with the Google Search tool (the runtime's service account)
  mock    - deterministic fake results (local development, tests)
  off     - disabled
  auto    - gemini if GOOGLE_AI_API_KEY is set, else vertex on GCP, else mock   (default)

The search model only finds and summarizes; the agent's own model writes the answer from the
results, under the agent's guardrails, with [n] citations to the pages found. Domain rules from the
functional context are applied in the query (site: / -site:) and again on the returned sources.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from eap_core import gcp

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


class SearchError(Exception):
    pass


@dataclass
class WebResult:
    title: str
    uri: str
    text: str


def _domain(s: str) -> str:
    s = s.strip().lower()
    host = urlparse(s).hostname if "://" in s else s.split("/")[0]
    return (host or "").removeprefix("www.")


def domain_ok(uri_or_title: str, allowed: list[str], blocked: list[str]) -> bool:
    d = _domain(uri_or_title)
    if not d:
        return not allowed
    match = lambda rule: d == _domain(rule) or d.endswith("." + _domain(rule))  # noqa: E731
    if any(match(b) for b in blocked):
        return False
    return not allowed or any(match(a) for a in allowed)


def scoped_query(query: str, allowed: list[str], blocked: list[str]) -> str:
    q = query.strip()
    if allowed:
        q += " (" + " OR ".join(f"site:{_domain(a)}" for a in allowed) + ")"
    for b in blocked:
        q += f" -site:{_domain(b)}"
    return q


class WebSearcher:
    def __init__(self, backend: str | None = None, project: str | None = None, location: str = "europe-west4",
                 model: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.backend = (backend or os.environ.get("EAP_WEB_SEARCH", "auto")).lower()
        self.project, self.location = project, location
        self.model = model or os.environ.get("EAP_WEB_SEARCH_MODEL", "gemini-2.5-flash")
        self.client = httpx.AsyncClient(transport=transport, timeout=60)

    def resolved_backend(self) -> str:
        if self.backend != "auto":
            return self.backend
        if os.environ.get("GOOGLE_AI_API_KEY"):
            return "gemini"
        if self.project or os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return "vertex"
        return "mock"

    async def search(self, query: str, *, allowed: list[str], blocked: list[str], max_results: int = 5,
                     language: str = "") -> list[WebResult]:
        backend = self.resolved_backend()
        if backend == "off":
            raise SearchError("web search is switched off on this runtime (EAP_WEB_SEARCH=off)")
        q = scoped_query(query, allowed, blocked)
        if backend == "mock":
            results = self._mock(query, allowed, max_results)
        elif backend in ("gemini", "vertex"):
            results = await self._grounded(q, backend, language)
        else:
            raise SearchError(f"unknown web search backend {backend}")
        kept = [r for r in results if not r.uri or domain_ok(r.uri if "grounding-api-redirect" not in r.uri else r.title,
                                                            allowed, blocked)]
        return kept[: max_results + 1]  # + the summary passage

    # ------------------------------------------------------------------ backends

    def _mock(self, query: str, allowed: list[str], n: int) -> list[WebResult]:
        site = _domain(allowed[0]) if allowed else "example.org"
        return [WebResult(title=f"{site}: result {i + 1} for {query[:60]}",
                          uri=f"https://{site}/search?q={quote_plus(query[:80])}&r={i + 1}",
                          text=f"(mock web result {i + 1}) Public information about: {query[:200]}")
                for i in range(min(n, 3))]

    async def _grounded(self, query: str, backend: str, language: str) -> list[WebResult]:
        prompt = ("Search the web and report the facts that answer this question, with the most relevant "
                  f"details (numbers, dates, conditions). Question: {query}")
        if language and language != "match the user":
            prompt += f"\nReport in {language}."
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.1}}
        if backend == "gemini":
            key = os.environ.get("GOOGLE_AI_API_KEY", "")
            if not key:
                raise SearchError("GOOGLE_AI_API_KEY is not set on the runtime")
            body["tools"] = [{"google_search": {}}]
            r = await self.client.post(f"{GEMINI_API}/models/{self.model}:generateContent", json=body,
                                       headers={"x-goog-api-key": key})
        else:
            project = self.project or await gcp.project_id()
            body["tools"] = [{"googleSearch": {}}]
            host = "https://aiplatform.googleapis.com" if self.location == "global" else f"https://{self.location}-aiplatform.googleapis.com"
            url = (f"{host}/v1/projects/{project}/locations/{self.location}/publishers/google/models/"
                   f"{self.model}:generateContent")
            r = await self.client.post(url, json=body, headers={"Authorization": f"Bearer {await gcp.access_token()}"})
        if r.status_code >= 400:
            raise SearchError(f"web search {r.status_code}: {r.text[:300]}")
        return self.parse(r.json())

    @staticmethod
    def parse(data: dict[str, Any]) -> list[WebResult]:
        cands = data.get("candidates") or []
        if not cands:
            return []
        c = cands[0]
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) or [] if not p.get("thought"))
        meta = c.get("groundingMetadata") or {}
        chunks = [ch.get("web") or {} for ch in meta.get("groundingChunks") or []]
        # snippets per source from groundingSupports (which part of the summary each source supports)
        support: dict[int, list[str]] = {}
        for s in meta.get("groundingSupports") or []:
            seg = (s.get("segment") or {}).get("text", "")
            for i in s.get("groundingChunkIndices") or []:
                support.setdefault(i, []).append(seg)
        out = [WebResult(title="Web search summary", uri="", text=re.sub(r"\s+", " ", text).strip()[:4000])] if text else []
        for i, w in enumerate(chunks):
            if w.get("uri"):
                out.append(WebResult(title=w.get("title") or _domain(w["uri"]), uri=w["uri"],
                                     text=" ".join(support.get(i, []))[:1500] or (w.get("title") or "")))
        return out
