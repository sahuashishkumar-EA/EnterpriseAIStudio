"""Model adapters with one normalized interface.

  vertex_gemini  Vertex AI generateContent (REST)
  vertex_claude  Claude on Vertex AI: Anthropic Messages API body via :rawPredict (REST)
  mock           deterministic local model (dev / tests / CI evals)

Adapters are deliberately REST-only (no vendor SDKs) and authenticate with the Cloud Run
service account via the metadata server.

Notes for Claude on Vertex: the body carries `anthropic_version` (default
"vertex-2023-10-16", overridable per catalog entry). Sampling params (temperature/top_p/
top_k) are not sent: newer Claude models reject them. Thinking is left at the model
default; `effort` may be set via catalog/definition params (sent as output_config.effort).
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from eap_core import gcp


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    signature: str | None = None  # Gemini thoughtSignature: must be returned unchanged in the same part


@dataclass
class Message:
    role: str  # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name for role=tool
    is_error: bool = False


@dataclass
class ModelRequest:
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None
    max_output_tokens: int = 2048
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end"  # end | tool_use | max_tokens | refusal
    model: str = ""


class ModelError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


UNUSABLE_STATUSES = ("retired", "blocked")


def cost_usd(entry: dict[str, Any], input_tokens: int, output_tokens: int) -> float:
    p = entry.get("pricing") or {}
    return input_tokens / 1e6 * float(p.get("input_per_mtok", 0)) + output_tokens / 1e6 * float(
        p.get("output_per_mtok", 0)
    )


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


# ------------------------------------------------------------------------- vertex


def _host(location: str, endpoint: str | None) -> str:
    if endpoint:
        return endpoint.rstrip("/")
    if location == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{location}-aiplatform.googleapis.com"


class _Vertex:
    def __init__(self, project: str | None, client: httpx.AsyncClient):
        self.project = project
        self.client = client

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await gcp.access_token()
        r = await self.client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (429, 500, 502, 503, 504):
            raise ModelError(f"vertex {r.status_code}: {r.text[:300]}", retryable=True, status=r.status_code)
        if r.status_code >= 400:
            raise ModelError(f"vertex {r.status_code}: {r.text[:500]}", status=r.status_code)
        return r.json()

    async def _project(self) -> str:
        if self.project is None:
            self.project = await gcp.project_id()
        return self.project


class GeminiAdapter(_Vertex):
    def build(self, entry: dict[str, Any], req: ModelRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "user":
                contents.append({"role": "user", "parts": [{"text": m.content}]})
            elif m.role == "assistant":
                parts: list[dict[str, Any]] = [{"text": m.content}] if m.content else []
                for c in m.tool_calls:
                    part: dict[str, Any] = {"functionCall": {"name": c.name, "args": c.arguments}}
                    if c.signature:
                        part["thoughtSignature"] = c.signature
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
            elif m.role == "tool":
                part = {"functionResponse": {"name": m.name, "response": {"content": m.content, "is_error": m.is_error}}}
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
        gen: dict[str, Any] = {"maxOutputTokens": req.max_output_tokens}
        for k_in, k_out in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
            if k_in in req.params:
                gen[k_out] = req.params[k_in]
        if req.json_schema is not None and not req.tools:
            gen["responseMimeType"] = "application/json"
            if req.params.get("response_json_schema", False):
                gen["responseJsonSchema"] = req.json_schema
        body: dict[str, Any] = {"contents": contents, "generationConfig": gen}
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        if req.tools:
            body["tools"] = [
                {"functionDeclarations": [{"name": t.name, "description": t.description, "parameters": t.parameters}
                                          for t in req.tools]}
            ]
        return body

    async def generate(self, entry: dict[str, Any], req: ModelRequest) -> ModelResponse:
        loc = entry.get("location") or "europe-west4"
        url = (
            f"{_host(loc, (entry.get('params') or {}).get('endpoint'))}/v1/projects/{await self._project()}"
            f"/locations/{loc}/publishers/google/models/{entry['model_id']}:generateContent"
        )
        data = await self._post(url, self.build(entry, req))
        return self.parse(entry, data)

    @staticmethod
    def parse(entry: dict[str, Any], data: dict[str, Any]) -> ModelResponse:
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback", {})
            return ModelResponse(text="", stop_reason="refusal" if fb.get("blockReason") else "end", model=entry["model_id"])
        c = cands[0]
        texts, calls = [], []
        for p in (c.get("content") or {}).get("parts", []) or []:
            if p.get("thought"):
                continue
            if "text" in p:
                texts.append(p["text"])
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(id=uuid.uuid4().hex[:12], name=fc["name"], arguments=fc.get("args") or {},
                                      signature=p.get("thoughtSignature")))
        fr = c.get("finishReason", "STOP")
        stop = {"MAX_TOKENS": "max_tokens", "SAFETY": "refusal", "PROHIBITED_CONTENT": "refusal",
                "BLOCKLIST": "refusal", "SPII": "refusal"}.get(fr, "tool_use" if calls else "end")
        u = data.get("usageMetadata") or {}
        return ModelResponse(
            text="".join(texts),
            tool_calls=calls,
            input_tokens=int(u.get("promptTokenCount", 0)),
            output_tokens=int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0)),
            stop_reason=stop,
            model=entry["model_id"],
        )


class GoogleAIAdapter(GeminiAdapter):
    """Gemini Developer API (Google AI Studio API key) - same request/response shape as Vertex
    generateContent, different endpoint and auth. Key from env GOOGLE_AI_API_KEY (Secret Manager
    on Cloud Run). Note: data handling and residency follow the Gemini API terms, not Vertex AI's."""

    BASE = "https://generativelanguage.googleapis.com/v1beta"

    async def generate(self, entry: dict[str, Any], req: ModelRequest) -> ModelResponse:
        import os

        key = os.environ.get("GOOGLE_AI_API_KEY", "")
        if not key:
            raise ModelError("GOOGLE_AI_API_KEY is not configured on the runtime")
        base = (entry.get("params") or {}).get("endpoint") or self.BASE
        r = await self.client.post(f"{base}/models/{entry['model_id']}:generateContent",
                                   json=self.build(entry, req), headers={"x-goog-api-key": key})
        if r.status_code in (429, 500, 502, 503, 504):
            raise ModelError(f"google ai {r.status_code}: {r.text[:300]}", retryable=True, status=r.status_code)
        if r.status_code >= 400:
            raise ModelError(f"google ai {r.status_code}: {r.text[:500]}", status=r.status_code)
        return self.parse(entry, r.json())


class ClaudeAdapter(_Vertex):
    def build(self, entry: dict[str, Any], req: ModelRequest) -> dict[str, Any]:
        params = {**(entry.get("params") or {}), **req.params}
        msgs: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "user":
                msgs.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
            elif m.role == "assistant":
                content: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content else []
                content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                msgs.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if m.is_error:
                    block["is_error"] = True
                # all tool results for one assistant turn go in a single user message
                if msgs and msgs[-1]["role"] == "user" and msgs[-1]["content"] and msgs[-1]["content"][0].get("type") == "tool_result":
                    msgs[-1]["content"].append(block)
                else:
                    msgs.append({"role": "user", "content": [block]})
        merged: list[dict[str, Any]] = []
        for m in msgs:  # the Messages API expects alternating roles
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"].extend(m["content"])
            else:
                merged.append(m)
        msgs = merged
        body: dict[str, Any] = {
            "anthropic_version": params.get("anthropic_version", "vertex-2023-10-16"),
            "max_tokens": req.max_output_tokens,
            "messages": msgs,
        }
        if req.system:
            body["system"] = req.system
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools]
        if params.get("effort"):
            body["output_config"] = {"effort": params["effort"]}
        return body

    async def generate(self, entry: dict[str, Any], req: ModelRequest) -> ModelResponse:
        loc = entry.get("location") or "europe-west4"
        url = (
            f"{_host(loc, (entry.get('params') or {}).get('endpoint'))}/v1/projects/{await self._project()}"
            f"/locations/{loc}/publishers/anthropic/models/{entry['model_id']}:rawPredict"
        )
        return self.parse(entry, await self._post(url, self.build(entry, req)))

    @staticmethod
    def parse(entry: dict[str, Any], data: dict[str, Any]) -> ModelResponse:
        texts, calls = [], []
        for b in data.get("content") or []:
            if b.get("type") == "text":
                texts.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                calls.append(ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {}))
        sr = data.get("stop_reason") or "end_turn"
        stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}.get(sr, "end")
        u = data.get("usage") or {}
        return ModelResponse(
            text="".join(texts),
            tool_calls=calls,
            input_tokens=int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0)
            + int(u.get("cache_creation_input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0)),
            stop_reason=stop,
            model=entry["model_id"],
        )


# --------------------------------------------------------------------------- mock


def _facts(messages: list[Message]) -> dict[str, Any]:
    """Collect key/value facts from JSON found in user and tool messages."""
    facts: dict[str, Any] = {}

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(x, (dict, list)) or k not in facts:
                    facts.setdefault(k, x)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for m in messages:
        if m.role not in ("user", "tool"):
            continue
        for blob in re.findall(r"\{.*\}|\[.*\]", m.content, flags=re.S):
            try:
                walk(json.loads(blob))
            except json.JSONDecodeError:
                continue
    return facts


def synthesize(schema: dict[str, Any], facts: dict[str, Any], key: str | None = None) -> Any:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if "enum" in schema:
        v = facts.get(key)
        return v if v in schema["enum"] else schema["enum"][0]
    if t == "object" or "properties" in schema:
        return {k: synthesize(s, facts, k) for k, s in (schema.get("properties") or {}).items()}
    if t == "array":
        v = facts.get(key)
        if isinstance(v, list):
            return v
        return [synthesize(schema.get("items") or {"type": "string"}, facts, key)]
    v = facts.get(key) if key else None
    if t == "string":
        return v if isinstance(v, str) else (str(v) if v is not None and not isinstance(v, (dict, list)) else f"mock-{key or 'value'}")
    if t == "number":
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 1.0
    if t == "integer":
        return int(v) if isinstance(v, int) and not isinstance(v, bool) else 1
    if t == "boolean":
        return v if isinstance(v, bool) else True
    return v if v is not None else None


class MockAdapter:
    """Deterministic behaviour:
      * tools offered and no tool results yet -> call every tool whose required args can be
        filled from facts in the conversation (max 3)
      * json_schema -> synthesize an object from the schema using conversation facts
      * otherwise -> short text answer citing any [n] sources present in the prompt
      * entry/definition param `mock_responses`: scripted list, indexed by assistant turn
    """

    async def generate(self, entry: dict[str, Any], req: ModelRequest) -> ModelResponse:
        prompt_text = req.system + "\n".join(m.content for m in req.messages)
        turn = sum(1 for m in req.messages if m.role == "assistant")
        scripted = (req.params.get("mock_responses") or (entry.get("params") or {}).get("mock_responses") or [])
        if turn < len(scripted):
            s = scripted[turn]
            calls = [ToolCall(id=f"call{turn}_{i}", name=c["name"], arguments=c.get("arguments", {}))
                     for i, c in enumerate(s.get("tool_calls", []))]
            text = s.get("text") if "text" in s else (json.dumps(s["json"]) if "json" in s else "")
            return self._resp(entry, prompt_text, text, calls, req.max_output_tokens)
        facts = _facts(req.messages)
        has_tool_results = any(m.role == "tool" for m in req.messages)
        if req.tools and not has_tool_results:
            calls = []
            for t in req.tools:
                req_args = (t.parameters or {}).get("required", []) or []
                props = (t.parameters or {}).get("properties", {}) or {}
                if req_args and all(a in facts for a in req_args):
                    args = {a: facts[a] for a in props if a in facts and not isinstance(facts[a], (dict, list))}
                    for a in req_args:
                        args[a] = facts[a]
                    calls.append(ToolCall(id=f"call_{len(calls)}", name=t.name, arguments=args))
                elif not req_args and "text" in props and "text" in facts:
                    calls.append(ToolCall(id=f"call_{len(calls)}", name=t.name, arguments={"text": facts["text"]}))
                if len(calls) >= 3:
                    break
            if calls:
                return self._resp(entry, prompt_text, "", calls, req.max_output_tokens)
        if req.json_schema is not None:
            return self._resp(entry, prompt_text, json.dumps(synthesize(req.json_schema, facts)), [], req.max_output_tokens)
        cites = sorted(set(re.findall(r"^\[(\d+)\]", "\n".join(m.content for m in req.messages), flags=re.M)), key=int)
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        summary = re.sub(r"\s+", " ", last_user)[:160]
        text = f"Mock answer ({entry.get('model_id', 'mock')}): {summary}"
        tool_bits = [m.content[:120] for m in req.messages if m.role == "tool"]
        if tool_bits:
            text += " | tool results: " + " ; ".join(tool_bits)
        if cites:
            text += " " + " ".join(f"[{c}]" for c in cites[:3])
        return self._resp(entry, prompt_text, text, [], req.max_output_tokens)

    @staticmethod
    def _resp(entry: dict[str, Any], prompt: str, text: str, calls: list[ToolCall], max_out: int) -> ModelResponse:
        out = estimate_tokens(text + json.dumps([c.arguments for c in calls]))
        return ModelResponse(
            text=text,
            tool_calls=calls,
            input_tokens=estimate_tokens(prompt),
            output_tokens=min(out, max_out),  # like a real model, never bills beyond max_output_tokens
            stop_reason="tool_use" if calls else "end",
            model=entry.get("model_id", "mock"),
        )


class ModelRegistry:
    def __init__(self, project: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(transport=transport, timeout=300)
        self.adapters = {
            "mock": MockAdapter(),
            "vertex_gemini": GeminiAdapter(project, self.client),
            "vertex_claude": ClaudeAdapter(project, self.client),
            "google_ai": GoogleAIAdapter(project, self.client),
        }

    async def generate(self, entry: dict[str, Any], req: ModelRequest) -> ModelResponse:
        if entry.get("status") in UNUSABLE_STATUSES:
            raise ModelError(f"model {entry.get('id')} is {entry['status']}")
        adapter = self.adapters.get(entry.get("provider", ""))
        if adapter is None:
            raise ModelError(f"no adapter for provider {entry.get('provider')}")
        return await adapter.generate(entry, req)
