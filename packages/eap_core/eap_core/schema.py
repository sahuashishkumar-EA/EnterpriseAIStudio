"""Agent definition schema, version 1.

A definition is the complete, declarative description of an agent. Published versions are
immutable; the Governor stores them by content hash and the Runtime executes them as-is.

Sections:
  contract    input/output JSON schemas (the public interface other agents depend on)
  prompt      system prompt, instructions and static context (soft controls)
  model       catalog model reference + generation params + fallbacks
  pattern     orchestration pattern and its params
  bindings    tools, knowledge sources and other agents this agent may use
  guardrails  hard controls enforced in code, per stage
  limits      cost / token / latency / tool-call / iteration / depth caps
  output      output format (text | json | markdown) and validation
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

PATTERNS = ("single_shot", "rag", "research_loop", "map_reduce", "human_in_the_loop")
STAGES = ("input", "retrieval", "tool_call", "tool_result", "output")
OUTPUT_FORMATS = ("text", "json", "markdown")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contract(_Model):
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {"text": {"type": "string"}}}
    )
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ContextItem(_Model):
    title: str
    text: str


class Prompt(_Model):
    system: str
    instructions: str = ""
    context: list[ContextItem] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)


class ModelRef(_Model):
    catalog_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    fallbacks: list[str] = Field(default_factory=list)


class Pattern(_Model):
    type: Literal["single_shot", "rag", "research_loop", "map_reduce", "human_in_the_loop"]
    params: dict[str, Any] = Field(default_factory=dict)


class ToolBinding(_Model):
    ref: str  # catalog tool id
    name: str | None = None  # name exposed to the model; defaults to ref
    params: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBinding(_Model):
    ref: str  # catalog knowledge source id
    top_k: int = Field(default=4, ge=1, le=50)
    min_score: float = 0.0


class AgentBinding(_Model):
    name: str
    alias: str = "prod"
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("invalid agent name")
        return v


class Bindings(_Model):
    tools: list[ToolBinding] = Field(default_factory=list)
    knowledge: list[KnowledgeBinding] = Field(default_factory=list)
    agents: list[AgentBinding] = Field(default_factory=list)


class Guardrail(_Model):
    type: str
    stage: Literal["input", "retrieval", "tool_call", "tool_result", "output"]
    # respond: stop and answer with params.message (friendly functional guardrails)
    action: Literal["block", "redact", "warn", "respond"] = "block"
    params: dict[str, Any] = Field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return (self.type, self.stage)


class Limits(_Model):
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_latency_ms: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_iterations: int | None = Field(default=None, ge=1)
    max_depth: int | None = Field(default=None, ge=0)


class Output(_Model):
    format: Literal["text", "json", "markdown"] = "text"
    json_schema: dict[str, Any] | None = None
    require_citations: bool = False


CONTEXT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


class Behavior(_Model):
    tone: Literal["friendly", "professional", "formal", "empathetic", "playful", "neutral"] = "professional"
    verbosity: Literal["brief", "balanced", "detailed"] = "balanced"
    language: str = "match the user"
    emoji: bool = False
    format: Literal["plain", "bullets", "steps", "auto"] = "auto"


class Note(_Model):
    title: str
    text: str


class WebSource(_Model):
    url: str
    max_pages: int = Field(default=5, ge=1, le=200)
    same_domain_only: bool = True
    # page: the page and a few linked pages (max 25). site: crawl the site smartly - sitemap first,
    # links ranked by relevance to the context, include/exclude URL patterns (max 200 pages).
    mode: Literal["page", "site"] = "page"
    include: list[str] = Field(default_factory=list)  # URL substrings / globs a page must match
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _pages(self) -> "WebSource":
        if self.mode == "page" and self.max_pages > 25:
            raise ValueError("page mode reads at most 25 pages; use site mode for more")
        return self

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("url must start with http:// or https://")
        return v


class WebSearch(_Model):
    """Live web search at answer time (any site, or only/never the listed domains)."""

    enabled: bool = False
    when: Literal["auto", "always"] = "auto"  # auto: only when the scanned knowledge does not cover it
    allowed_domains: list[str] = Field(default_factory=list)  # empty = the whole web
    blocked_domains: list[str] = Field(default_factory=list)
    max_results: int = Field(default=5, ge=1, le=10)


class Escalation(_Model):
    triggers: list[str] = Field(default_factory=list)
    message: str = "I'll connect you with a colleague who can help with this."


class FunctionalGuardrails(_Model):
    allowed_topics: list[str] = Field(default_factory=list)  # soft: stay on these topics
    blocked_topics: list[str] = Field(default_factory=list)  # hard: polite refusal
    refusal_message: str = "Sorry, I can't help with that topic. Is there something else I can do for you?"
    escalation: Escalation = Field(default_factory=Escalation)
    personal_data: Literal["allow", "redact", "refuse"] = "redact"
    disclaimer: str = ""


class Example(_Model):
    user: str
    assistant: str


class FunctionalContext(_Model):
    """Business-owned behaviour of the agent for one audience / use case. An agent can have
    several; the caller selects one per conversation (default_context otherwise)."""

    id: str
    name: str
    description: str = ""
    audience: str = ""
    role: str = ""
    goal: str = ""
    behavior: Behavior = Field(default_factory=Behavior)
    always: list[str] = Field(default_factory=list)
    never: list[str] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    web_sources: list[WebSource] = Field(default_factory=list)
    web_refresh_days: int = Field(default=0, ge=0, le=90)  # re-scan web sources every N days (0 = manual)
    web_search: WebSearch = Field(default_factory=WebSearch)
    guardrails: FunctionalGuardrails = Field(default_factory=FunctionalGuardrails)
    examples: list[Example] = Field(default_factory=list)
    starters: list[str] = Field(default_factory=list)
    owner: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not CONTEXT_ID_RE.match(v):
            raise ValueError("context id must match ^[a-z][a-z0-9-]{0,39}$")
        return v


class Exposure(_Model):
    mcp: bool = False  # expose through the runtime's MCP server
    chat: bool = False  # offer in the chat app
    callable_by_agents: bool = True
    # Who may invoke directly: "owner_bu" (anyone with a role in the owner BU),
    # "authenticated", "bu:<name>", "user:<email>". Trusted service callers always may.
    invokers: list[str] = Field(default_factory=lambda: ["owner_bu"])


HOSTING_PLATFORMS = ("internal", "copilot_studio", "http", "a2a")


class Hosting(_Model):
    """Where the agent actually runs. "internal" = the platform runtime executes it. Any other platform
    means the agent is built and run elsewhere (e.g. Microsoft Copilot Studio) and the governor still
    owns its behaviour: the external agent pulls its published profile, calls the check APIs around
    each turn, reports runs, and is tested through `endpoint` before every go-live."""

    platform: Literal["internal", "copilot_studio", "http", "a2a"] = "internal"
    # http: URL that takes {"message", "context", "history"} and returns {"output"} (or {"reply"}).
    # copilot_studio: Direct Line base URL (empty = https://directline.botframework.com/v3/directline).
    endpoint: str = ""
    # Name of a secret (environment variable / Secret Manager secret mounted as one) with the bearer
    # token or Direct Line secret for `endpoint`. Never the secret itself.
    auth_secret: str = ""
    # Principals allowed to use this agent's governance API (/gov/v1): service names, OIDC client ids
    # (e.g. the Entra app id of a Copilot Studio connector) or user:<email>.
    callers: list[str] = Field(default_factory=list)
    # Called (HMAC-SHA256 signed) whenever a new version goes live, so the external platform can refresh.
    webhook_url: str = ""
    webhook_secret: str = ""  # secret name, as auth_secret
    notes: str = ""

    @property
    def external(self) -> bool:
        return self.platform != "internal"


class Customization(_Model):
    """On a core agent: what business units may change in their own variant of it.
    The technical owner keeps the system prompt, orchestration, tools and guardrails."""

    enabled: bool = False
    allowed_bus: list[str] = Field(default_factory=lambda: ["*"])
    models: list[str] = Field(default_factory=list)  # catalog ids a BU may pick (empty = keep the core model)
    instructions: bool = True  # BU rules appended to the core prompt
    contract: bool = False  # BU-specific input/output contract
    output: bool = True  # output format / citations
    knowledge: bool = True  # BU knowledge sources and web scans
    web_search: bool = False  # live web search in BU contexts


class Extends(_Model):
    agent: str
    version: str  # the core version this variant is built and tested on


class Overlay(_Model):
    """A BU variant's own settings on top of the core (validated against the core's customization)."""

    instructions: str = ""
    model: ModelRef | None = None
    contract: Contract | None = None
    output: Output | None = None
    limits: Limits = Field(default_factory=Limits)  # can only tighten
    guardrails: list[Guardrail] = Field(default_factory=list)  # added to the core's
    knowledge: list[KnowledgeBinding] = Field(default_factory=list)  # added to the core's


class AgentDefinition(_Model):
    schema_version: Literal["1"] = SCHEMA_VERSION
    name: str
    description: str = ""
    owner_bu: str
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    contract: Contract = Field(default_factory=Contract)
    prompt: Prompt
    model: ModelRef
    pattern: Pattern = Field(default_factory=lambda: Pattern(type="single_shot"))
    bindings: Bindings = Field(default_factory=Bindings)
    guardrails: list[Guardrail] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    output: Output = Field(default_factory=Output)
    exposure: Exposure = Field(default_factory=Exposure)
    contexts: list[FunctionalContext] = Field(default_factory=list)
    default_context: str | None = None
    hosting: Hosting = Field(default_factory=Hosting)
    customization: Customization = Field(default_factory=Customization)  # core agents
    extends: Extends | None = None  # BU variants: the core agent and version
    overlay: Overlay = Field(default_factory=Overlay)  # BU variants
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must match ^[a-z][a-z0-9-]{1,62}$")
        return v

    @model_validator(mode="after")
    def _contexts(self) -> "AgentDefinition":
        ids = [c.id for c in self.contexts]
        if len(set(ids)) != len(ids):
            raise ValueError("functional context ids must be unique")
        if self.default_context is not None and self.default_context not in ids:
            raise ValueError(f"default_context {self.default_context!r} is not a defined context")
        return self

    def context(self, context_id: str | None) -> "FunctionalContext | None":
        """Selected context, else the default, else the first, else None."""
        if context_id:
            for c in self.contexts:
                if c.id == context_id:
                    return c
            raise ValueError(f"unknown context {context_id!r}")
        if self.default_context:
            return self.context(self.default_context)
        return self.contexts[0] if self.contexts else None

    def canonical(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=False)
        # Fields added after v1 are left out while they hold their defaults, so existing
        # definitions keep their content hashes (and published versions stay verifiable).
        for key, default in (("hosting", Hosting()), ("customization", Customization()), ("overlay", Overlay())):
            if data.get(key) == default.model_dump(mode="json"):
                data.pop(key)
        if data.get("extends") is None:
            data.pop("extends", None)
        ws_default, src_defaults = WebSearch().model_dump(mode="json"), {"mode": "page", "include": [], "exclude": []}
        for c in data.get("contexts") or []:
            if c.get("web_search") == ws_default:
                c.pop("web_search")
            if c.get("web_refresh_days") == 0:
                c.pop("web_refresh_days")
            for w in c.get("web_sources") or []:
                for k, v in src_defaults.items():
                    if w.get(k) == v:
                        w.pop(k)
        return data

    @property
    def is_variant(self) -> bool:
        return self.extends is not None

    def content_hash(self) -> str:
        return content_hash(self.canonical())


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(data: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(data).encode()).hexdigest()


def parse_definition(data: dict[str, Any]) -> AgentDefinition:
    return AgentDefinition.model_validate(data)


# ---------------------------------------------------------------------------- semver


def parse_semver(v: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(v)
    if not m:
        raise ValueError(f"not a semver: {v!r}")
    return int(m[1]), int(m[2]), int(m[3])


def bump(v: str | None, part: str) -> str:
    if v is None:
        return "1.0.0"
    major, minor, patch = parse_semver(v)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump {part!r}")


BUMP_ORDER = {"patch": 0, "minor": 1, "major": 2}


# ---------------------------------------------------------------- breaking changes


def _props(schema: dict[str, Any]) -> dict[str, Any]:
    return (schema or {}).get("properties", {}) or {}


def _required(schema: dict[str, Any]) -> set[str]:
    return set((schema or {}).get("required", []) or [])


def _schema_diff(old: dict, new: dict, path: str, direction: str, out: list[str]) -> None:
    """direction='output': removing/retyping fields breaks consumers.
    direction='input': adding required fields or retyping/removing accepted fields breaks callers."""
    old_t, new_t = (old or {}).get("type"), (new or {}).get("type")
    if old_t and new_t and old_t != new_t:
        out.append(f"{direction} {path or '<root>'}: type changed {old_t} -> {new_t}")
        return
    op, np_ = _props(old), _props(new)
    if direction == "output":
        for k in op:
            if k not in np_:
                out.append(f"output {path}{k}: field removed")
        for k in _required(old) - _required(new):
            if k in np_:
                out.append(f"output {path}{k}: no longer guaranteed (removed from required)")
    else:
        for k in _required(new) - _required(old):
            out.append(f"input {path}{k}: new required field")
        for k in op:
            if k not in np_ and (new or {}).get("additionalProperties") is False:
                out.append(f"input {path}{k}: field no longer accepted")
    for k in set(op) & set(np_):
        _schema_diff(op[k], np_[k], f"{path}{k}.", direction, out)
    o_enum, n_enum = (old or {}).get("enum"), (new or {}).get("enum")
    if o_enum and n_enum:
        removed = set(map(str, o_enum)) - set(map(str, n_enum))
        added = set(map(str, n_enum)) - set(map(str, o_enum))
        if direction == "input" and removed:
            out.append(f"input {path or '<root>'}: enum values removed {sorted(removed)}")
        if direction == "output" and added:
            out.append(f"output {path or '<root>'}: enum values added {sorted(added)}")


def breaking_changes(old: AgentDefinition, new: AgentDefinition) -> list[str]:
    """Changes that break callers of the agent's contract => require a major version."""
    out: list[str] = []
    _schema_diff(old.contract.input_schema, new.contract.input_schema, "", "input", out)
    old_out = old.output.json_schema or old.contract.output_schema
    new_out = new.output.json_schema or new.contract.output_schema
    _schema_diff(old_out, new_out, "", "output", out)
    if old.output.format != new.output.format:
        out.append(f"output format changed {old.output.format} -> {new.output.format}")
    if old.exposure.callable_by_agents and not new.exposure.callable_by_agents:
        out.append("no longer callable by agents")
    if old.exposure.mcp and not new.exposure.mcp:
        out.append("no longer exposed over MCP")
    removed = {c.id for c in old.contexts} - {c.id for c in new.contexts}
    for cid in sorted(removed):
        out.append(f"functional context {cid!r} removed (callers may select it)")
    return out


def required_bump(old: AgentDefinition | None, new: AgentDefinition) -> str:
    if old is None:
        return "major"
    if breaking_changes(old, new):
        return "major"
    o, n = old.canonical(), new.canonical()
    if {c.id for c in new.contexts} - {c.id for c in old.contexts}:
        return "minor"
    for section in ("contract", "bindings", "pattern", "output", "model", "exposure"):
        if o.get(section) != n.get(section):
            return "minor"
    return "patch"
