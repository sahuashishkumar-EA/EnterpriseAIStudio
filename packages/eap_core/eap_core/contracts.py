"""Invoke contract shared by callers, the runtime, the governor's eval service and MCP."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["ok", "partial", "failed", "needs_input"]


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["sync", "async", "stream"] = "sync"
    session_id: str | None = None
    idempotency_key: str | None = None
    budget_usd: float | None = Field(default=None, gt=0)
    callback_url: str | None = None
    context: str | None = None  # functional context id; default context when omitted
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)  # e.g. {"approved": true} or {"feedback": "..."}


class Source(BaseModel):
    id: str
    title: str = ""
    uri: str = ""
    snippet: str = ""
    score: float | None = None
    knowledge_ref: str | None = None


class Cost(BaseModel):
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0

    def add(self, other: "Cost") -> None:
        self.usd += other.usd
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls


class Step(BaseModel):
    kind: str  # model | tool | agent | guardrail | retrieval | info
    name: str = ""
    status: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


class Trace(BaseModel):
    trace_id: str
    run_id: str
    parent_run_id: str | None = None
    agent: str
    version: str
    alias: str | None = None
    content_hash: str | None = None
    depth: int = 0
    steps: list[Step] = Field(default_factory=list)


class ErrorInfo(BaseModel):
    code: str
    message: str


class NeedsInput(BaseModel):
    prompt: str
    draft: Any = None
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}, "feedback": {"type": "string"}},
        }
    )


class InvokeResponse(BaseModel):
    run_id: str
    status: Status
    output: Any = None
    sources: list[Source] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    latency_ms: int = 0
    trace: Trace
    error: ErrorInfo | None = None
    needs_input: NeedsInput | None = None
    warnings: list[str] = Field(default_factory=list)
    context: str | None = None
    flags: list[str] = Field(default_factory=list)  # e.g. refused:topic_policy, escalated
