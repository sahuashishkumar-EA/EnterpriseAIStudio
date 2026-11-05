"""Plugin contracts.

Each plugin (pattern, guardrail, tool kind, knowledge kind, model provider, eval assertion)
declares a JSON schema for its params. The Governor validates definitions against these
schemas and the console generates its forms from them (GET /api/v1/plugins), so adding a
plugin here makes it authorable without UI changes. Implementations live in the Runtime
(patterns, guardrails, tools, models) or the Governor (assertions).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import jsonschema

KINDS = ("pattern", "guardrail", "tool", "knowledge", "model_provider", "assertion")


@dataclass(frozen=True)
class PluginSpec:
    kind: str
    type: str
    title: str
    description: str
    params_schema: dict[str, Any]
    stages: tuple[str, ...] = ()  # guardrails only

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stages"] = list(self.stages)
        return d


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_INT = {"type": "integer", "minimum": 0}
_NUM = {"type": "number"}

_SPECS: list[PluginSpec] = [
    # ------------------------------------------------------------------ patterns
    PluginSpec("pattern", "single_shot", "Single shot", "One model call with prompt, context and input.", _obj({})),
    PluginSpec(
        "pattern",
        "rag",
        "Retrieval-augmented generation",
        "Retrieve from bound knowledge sources, then answer citing sources as [n].",
        _obj(
            {
                "query_path": {**_STR, "description": "Dotted path in input used as the search query (default: whole input)"},
                "max_context_chars": {**_INT, "default": 12000},
            }
        ),
    ),
    PluginSpec(
        "pattern",
        "research_loop",
        "Research loop (tools + agents)",
        "Model-driven loop calling bound tools and agents until it answers or hits limits.",
        _obj(
            {
                "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
                "parallel_tool_calls": {"type": "boolean", "default": True},
            }
        ),
    ),
    PluginSpec(
        "pattern",
        "map_reduce",
        "Map / reduce",
        "Fan an input list out to a child agent or prompt, then reduce the results.",
        _obj(
            {
                "items_path": {**_STR, "description": "Dotted path of the list in the input"},
                "map_agent": {**_STR, "description": "Bound agent to call per item (optional)"},
                "map_prompt": {**_STR, "description": "Prompt per item when no map_agent"},
                "reduce_prompt": {**_STR, "description": "Prompt combining mapped results"},
                "concurrency": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
            ["items_path"],
        ),
    ),
    PluginSpec(
        "pattern",
        "human_in_the_loop",
        "Human in the loop",
        "Draft an answer, pause with status needs_input, resume with approval or feedback.",
        _obj(
            {
                "review_prompt": {**_STR, "default": "Please review the draft. Approve, or give feedback."},
                "max_revisions": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
            }
        ),
    ),
    # ---------------------------------------------------------------- guardrails
    PluginSpec(
        "guardrail",
        "pii_redaction",
        "PII detection / redaction",
        "Detects emails, phone numbers, IBANs, credit cards (Luhn), national IDs.",
        _obj({"entities": {"type": "array", "items": {"enum": ["email", "phone", "iban", "credit_card", "ip_address", "nl_bsn"]}},
              "message": {**_STR, "description": "Reply used with action 'respond'"}}),
        stages=("input", "retrieval", "tool_call", "tool_result", "output"),
    ),
    PluginSpec(
        "guardrail",
        "prompt_injection",
        "Prompt-injection heuristics",
        "Flags instruction-override and exfiltration phrasing.",
        _obj({"extra_patterns": _STRS}),
        stages=("input", "retrieval", "tool_result"),
    ),
    PluginSpec(
        "guardrail",
        "topic_denylist",
        "Topic deny-list",
        "Blocks text mentioning any listed term (case-insensitive, word boundary).",
        _obj({"terms": _STRS}, ["terms"]),
        stages=("input", "retrieval", "tool_result", "output"),
    ),
    PluginSpec(
        "guardrail",
        "topic_policy",
        "Refused topics (friendly)",
        "If the user raises a listed topic, answer with the refusal message instead of calling the model.",
        _obj({"terms": _STRS, "message": _STR}, ["terms"]),
        stages=("input",),
    ),
    PluginSpec(
        "guardrail",
        "escalation",
        "Escalation / human hand-off",
        "If a trigger phrase appears, answer with the hand-off message and flag the run as escalated.",
        _obj({"terms": _STRS, "message": _STR}, ["terms"]),
        stages=("input",),
    ),
    PluginSpec(
        "guardrail",
        "regex_block",
        "Regex block",
        "Blocks or redacts text matching any pattern.",
        _obj({"patterns": _STRS}, ["patterns"]),
        stages=("input", "retrieval", "tool_call", "tool_result", "output"),
    ),
    PluginSpec(
        "guardrail",
        "max_length",
        "Maximum length",
        "Caps text length in characters.",
        _obj({"max_chars": {"type": "integer", "minimum": 1}}, ["max_chars"]),
        stages=("input", "tool_result", "output"),
    ),
    PluginSpec(
        "guardrail",
        "json_schema",
        "JSON schema",
        "Output must be JSON valid against the schema (defaults to the definition's output schema).",
        _obj({"schema": {"type": "object"}}),
        stages=("output",),
    ),
    PluginSpec(
        "guardrail",
        "citation_required",
        "Citations required",
        "Output must cite at least N retrieved sources.",
        _obj({"min": {"type": "integer", "minimum": 1, "default": 1}}),
        stages=("output",),
    ),
    PluginSpec(
        "guardrail",
        "tool_allowlist",
        "Tool allow-list",
        "Only the listed tools/agents may be called.",
        _obj({"tools": _STRS}, ["tools"]),
        stages=("tool_call",),
    ),
    # --------------------------------------------------------------------- tools
    PluginSpec(
        "tool",
        "builtin",
        "Built-in tool",
        "Runtime-provided tool implemented in code.",
        _obj({"function": {"enum": ["echo", "calculator", "current_time", "crm_lookup"]}}, ["function"]),
    ),
    PluginSpec(
        "tool",
        "rest",
        "REST API",
        "HTTP call to an internal API. Caller identity forwarded as X-On-Behalf-Of.",
        _obj(
            {
                "base_url": _STR,
                "method": {"enum": ["GET", "POST", "PUT", "PATCH"]},
                "path": {**_STR, "description": "May contain {param} placeholders"},
                "auth": {"enum": ["none", "google_id_token"], "default": "google_id_token"},
                "input_schema": {"type": "object"},
                "timeout_s": {**_NUM, "default": 20},
                "allowed_hosts": _STRS,
            },
            ["base_url", "method", "path"],
        ),
    ),
    PluginSpec(
        "tool",
        "mcp",
        "MCP server tool",
        "Calls a tool on a remote MCP server over streamable HTTP.",
        _obj(
            {
                "url": _STR,
                "tool": {**_STR, "description": "Remote tool name"},
                "auth": {"enum": ["none", "google_id_token"], "default": "google_id_token"},
                "timeout_s": {**_NUM, "default": 30},
            },
            ["url", "tool"],
        ),
    ),
    PluginSpec(
        "tool",
        "bigquery",
        "BigQuery (read-only)",
        "Parameterised read-only query. The model supplies parameters, not SQL.",
        _obj(
            {
                "project": _STR,
                "sql": {**_STR, "description": "SELECT with @named parameters"},
                "parameters": {
                    "type": "object",
                    "description": "name -> BigQuery type (STRING, INT64, FLOAT64, BOOL, DATE)",
                    "additionalProperties": {"type": "string"},
                },
                "max_bytes_billed": {"type": "integer", "minimum": 1, "default": 1_000_000_000},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 200},
                "location": {**_STR, "default": "EU"},
            },
            ["project", "sql"],
        ),
    ),
    # ----------------------------------------------------------------- knowledge
    PluginSpec(
        "knowledge",
        "pgvector",
        "pgvector collection",
        "Chunks + embeddings in the runtime's Postgres (pgvector).",
        _obj(
            {
                "embedding_model": {**_STR, "default": "mock-embedding"},
                "dimensions": {"type": "integer", "default": 768},
            }
        ),
    ),
    # ----------------------------------------------------------- model providers
    PluginSpec(
        "model_provider",
        "vertex_gemini",
        "Vertex AI Gemini",
        "generateContent on Vertex AI.",
        _obj({"location": _STR, "model_id": _STR, "endpoint": _STR}, ["model_id"]),
    ),
    PluginSpec(
        "model_provider",
        "vertex_claude",
        "Claude on Vertex AI",
        "Anthropic Messages API via Vertex rawPredict.",
        _obj({"location": _STR, "model_id": _STR, "endpoint": _STR, "anthropic_version": _STR}, ["model_id"]),
    ),
    PluginSpec(
        "model_provider",
        "google_ai",
        "Gemini API (Google AI Studio key)",
        "generateContent on generativelanguage.googleapis.com with an API key (GOOGLE_AI_API_KEY on the runtime).",
        _obj({"model_id": _STR, "location": _STR, "endpoint": _STR}, ["model_id"]),
    ),
    PluginSpec("model_provider", "mock", "Mock model", "Deterministic local model for dev/tests.", _obj({"model_id": _STR})),
    # ----------------------------------------------------------- eval assertions
    PluginSpec("assertion", "status", "Status equals", "Run status equals value.", _obj({"equals": {"enum": ["ok", "partial", "failed", "needs_input"]}}, ["equals"])),
    PluginSpec("assertion", "equals", "Field equals", "Value at path equals value (or value_from expected.*).", _obj({"path": _STR, "value": {}, "value_from": _STR}, ["path"])),
    PluginSpec("assertion", "contains", "Contains", "String/list at path contains value.", _obj({"path": _STR, "value": {}, "value_from": _STR, "case_sensitive": {"type": "boolean"}}, ["path"])),
    PluginSpec("assertion", "not_contains", "Does not contain", "String/list at path does not contain value.", _obj({"path": _STR, "value": {}, "value_from": _STR, "case_sensitive": {"type": "boolean"}}, ["path"])),
    PluginSpec("assertion", "regex", "Regex match", "String at path matches pattern.", _obj({"path": _STR, "pattern": _STR}, ["path", "pattern"])),
    PluginSpec("assertion", "min_items", "Minimum items", "List at path has at least min items.", _obj({"path": _STR, "min": _INT}, ["path", "min"])),
    PluginSpec("assertion", "max_cost_usd", "Max cost", "Run cost <= max.", _obj({"max": _NUM}, ["max"])),
    PluginSpec("assertion", "max_latency_ms", "Max latency", "Run latency <= max ms.", _obj({"max": _NUM}, ["max"])),
    PluginSpec("assertion", "citations", "Citations", "At least min sources returned.", _obj({"min": {"type": "integer", "minimum": 1, "default": 1}})),
    PluginSpec(
        "assertion",
        "llm_judge",
        "LLM judge",
        "A judge model scores the output 0..1 against a rubric.",
        _obj({"rubric": _STR, "model": _STR, "threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7}}, ["rubric"]),
    ),
]

_REGISTRY: dict[tuple[str, str], PluginSpec] = {(s.kind, s.type): s for s in _SPECS}


def register(spec: PluginSpec) -> None:
    if spec.kind not in KINDS:
        raise ValueError(f"unknown plugin kind {spec.kind}")
    _REGISTRY[(spec.kind, spec.type)] = spec


def get(kind: str, type_: str) -> PluginSpec | None:
    return _REGISTRY.get((kind, type_))


def list_specs(kind: str | None = None) -> list[PluginSpec]:
    return [s for s in _REGISTRY.values() if kind is None or s.kind == kind]


def validate_params(kind: str, type_: str, params: dict[str, Any]) -> list[str]:
    spec = get(kind, type_)
    if spec is None:
        return [f"unknown {kind} type {type_!r}"]
    validator = jsonschema.Draft202012Validator(spec.params_schema)
    return [
        f"{'.'.join(map(str, e.absolute_path)) or '<params>'}: {e.message}"
        for e in sorted(validator.iter_errors(params or {}), key=lambda e: list(e.absolute_path))
    ]


def catalog_json() -> dict[str, Any]:
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for s in _REGISTRY.values():
        out[s.kind].append(s.to_dict())
    return out
