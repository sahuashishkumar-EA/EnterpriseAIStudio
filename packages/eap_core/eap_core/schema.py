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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    action: Literal["block", "redact", "warn"] = "block"
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


class Exposure(_Model):
    mcp: bool = False  # expose through the runtime's MCP server
    callable_by_agents: bool = True
    # Who may invoke directly: "owner_bu" (anyone with a role in the owner BU),
    # "authenticated", "bu:<name>", "user:<email>". Trusted service callers always may.
    invokers: list[str] = Field(default_factory=lambda: ["owner_bu"])


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
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must match ^[a-z][a-z0-9-]{1,62}$")
        return v

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

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
    return out


def required_bump(old: AgentDefinition | None, new: AgentDefinition) -> str:
    if old is None:
        return "major"
    if breaking_changes(old, new):
        return "major"
    o, n = old.canonical(), new.canonical()
    for section in ("contract", "bindings", "pattern", "output", "model", "exposure"):
        if o.get(section) != n.get(section):
            return "minor"
    return "patch"
