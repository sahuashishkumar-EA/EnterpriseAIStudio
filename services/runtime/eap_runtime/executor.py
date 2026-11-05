"""Execution of one agent definition under one call context.

The executor owns every hard control at run time:
  * limits      cost (per run, carved from the caller's budget), input/output tokens,
                latency (deadline), tool calls, iterations, depth (via CallContext)
  * budgets     BU daily budget, reserved atomically before each model call
  * guardrails  enforced by stage (input, retrieval, tool_call, tool_result, output)
  * models      catalog entry status, fallbacks, retries
  * agents      child calls inherit identity, trace and budget; cycles/depth rejected
Soft controls (policy prompt text) are appended to the system prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

import jsonschema

from eap_core import compose
from eap_core.context import BudgetExceeded, CallContext, CallContextError
from eap_core.contracts import Cost, ErrorInfo, InvokeResponse, NeedsInput, Source, Step, Trace
from eap_core.schema import AgentBinding, AgentDefinition, parse_definition

from .budgets import BudgetExhausted, Budgets, bu_day_key
from .guardrails import Enforcer, GuardrailBlocked, PolicyResponse
from .knowledge import KnowledgeStore
from .models import (
    UNUSABLE_STATUSES,
    Message,
    ModelError,
    ModelRegistry,
    ModelRequest,
    ModelResponse,
    ToolSpec,
    cost_usd,
    estimate_tokens,
)
from .resolver import Resolver, pick_release
from .runs import RunStore
from .tools import ToolError, ToolRunner, tool_spec

log = logging.getLogger("eap.executor")


class LimitExceeded(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class InvalidInput(Exception):
    pass


class OutputInvalid(Exception):
    pass


class Refused(Exception):
    pass


class NeedsInputSignal(Exception):
    def __init__(self, prompt: str, draft: Any, state: dict[str, Any]):
        super().__init__(prompt)
        self.prompt = prompt
        self.draft = draft
        self.state = state


@dataclass
class Resume:
    input: dict[str, Any]
    state: dict[str, Any]


@dataclass
class Services:
    resolver: Resolver
    models: ModelRegistry
    tools: ToolRunner
    knowledge: KnowledgeStore
    budgets: Budgets
    runs: RunStore


Emitter = Callable[[str, dict[str, Any]], Awaitable[None]]

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_text(text: str) -> Any:
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s:e + 1])
        raise


def get_path(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


@dataclass
class Execution:
    svc: Services
    release: dict[str, Any]
    policy: dict[str, Any]
    ctx: CallContext
    agent: str
    alias: str | None = None
    owner_bu: str | None = None
    emitter: Emitter | None = None
    mode: str = "sync"
    resume: Resume | None = None
    context_id: str | None = None  # functional context selected by the caller
    history: list[dict[str, str]] = field(default_factory=list)  # prior chat turns [{role, content}]

    defn: AgentDefinition = field(init=False)
    cost: Cost = field(default_factory=Cost)
    steps: list[Step] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    state: dict[str, Any] | None = None
    partial: Any = None
    force_partial: bool = False
    flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.defn = parse_definition(self.release["definition"])
        self.fctx = self.defn.context(self.context_id)  # ValueError for an unknown context id
        self.limits = self.release["limits"]
        # technical + platform guardrails from the bundle, plus the functional context's own
        self.enforcer = Enforcer([*(self.release.get("guardrails") or []), *compose.functional_guardrails(self.fctx)])
        self.catalog = self.release.get("catalog") or {"models": {}, "tools": {}, "knowledge": {}}
        self.budget_usd = min(self.ctx.budget_usd, float(self.limits["max_cost_usd"]))
        self.started = time.time()
        deadline = self.started + float(self.limits["max_latency_ms"]) / 1000.0
        self.deadline = min(deadline, self.ctx.deadline) if self.ctx.deadline else deadline
        self.tool_calls = 0
        self.owner_bu = self.owner_bu or self.defn.owner_bu
        self._toolset: tuple[list[ToolSpec], dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]] | None = None

    # ----------------------------------------------------------------- bookkeeping

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_usd - self.cost.usd)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    async def step(self, kind: str, name: str = "", status: str = "ok", t0: float | None = None, **detail: Any) -> None:
        s = Step(kind=kind, name=name, status=status, detail=detail,
                 latency_ms=int((time.time() - t0) * 1000) if t0 else 0)
        self.steps.append(s)
        if self.emitter:
            await self.emitter("step", {"agent": self.agent, "depth": self.ctx.depth, **s.model_dump()})

    def check_limits(self) -> None:
        if time.time() > self.deadline:
            raise LimitExceeded("latency_exceeded", f"max_latency_ms {self.limits['max_latency_ms']} exceeded")
        if self.remaining_usd <= 0:
            raise BudgetExceeded(f"run budget ${self.budget_usd:.4f} exhausted")

    @property
    def wants_json(self) -> bool:
        return self.defn.output.format == "json"

    def output_schema(self) -> dict[str, Any] | None:
        return self.defn.output.json_schema or self.defn.contract.output_schema or None

    def system_prompt(self) -> str:
        return compose.system_prompt(self.defn, self.fctx, self.release.get("soft_controls") or [],
                                     self.output_schema() if self.wants_json else None)

    def render_input(self, value: Any) -> str:
        # chat-style input ({"message": "..."}) goes to the model as plain text
        if isinstance(value, dict) and len(value) == 1:
            (k, v), = value.items()
            if k in ("message", "text", "question") and isinstance(v, str):
                return v
        return "Input (JSON):\n" + json.dumps(value, ensure_ascii=False, indent=2, default=str)

    def user_text(self, value: Any) -> str:
        if isinstance(value, dict):
            for k in ("message", "question", "query", "text"):
                if isinstance(value.get(k), str):
                    return value[k]
        return json.dumps(value, ensure_ascii=False, default=str)

    def conversation(self, current: str) -> list[Message]:
        """Prior turns (input guardrails re-applied to user turns) + the current user message."""
        msgs: list[Message] = []
        for t in self.history[-30:]:
            content = t.get("content", "")
            if t.get("role") == "user":
                try:
                    content = self.enforcer.text("input", content, raise_on_block=False).text
                except PolicyResponse:
                    pass
            msgs.append(Message("assistant" if t.get("role") == "assistant" else "user", content))
        msgs.append(Message("user", current))
        return msgs

    async def grounding(self, query: str) -> str:
        """Reference material from the functional context's scanned web pages ([n] citations)."""
        refs = compose.knowledge_refs(self.defn, self.fctx)
        if not refs:
            return ""
        t0 = time.time()
        lines: list[str] = []
        for ref in refs:
            src = self.catalog.get("knowledge", {}).get(ref)
            if src is None:
                self.warn(f"web sources of context {self.fctx.id!r} have not been scanned yet")
                continue
            try:
                hits = await self.svc.knowledge.search(src, query, 4, 0.05)
            except Exception as e:  # knowledge must not break the conversation
                self.warn(f"knowledge search failed: {e}")
                continue
            for h in hits:
                o = self.enforcer.text("retrieval", h["content"], raise_on_block=False)
                if o.blocked:
                    continue
                n = len(self.sources) + 1
                self.sources.append(Source(id=h["id"], title=h.get("title", ""), uri=h.get("uri", ""),
                                           snippet=o.text[:300], score=round(float(h["score"]), 4), knowledge_ref=ref))
                lines.append(f"[{n}] {h.get('title') or h.get('uri')} ({h.get('uri', '')})\n{o.text[:2500]}")
        await self.step("retrieval", ",".join(refs), "ok", t0, hits=len(lines))
        if not lines:
            return ""
        return ("\n\n---\nReference material (use it when relevant; cite as [n]; if it does not cover the "
                "question, say so rather than guessing):\n\n" + "\n\n".join(lines))

    # ----------------------------------------------------------------------- model

    async def model(self, messages: list[Message], *, tools: list[ToolSpec] | None = None,
                    json_mode: bool = False, max_tokens: int | None = None) -> ModelResponse:
        self.check_limits()
        system = self.system_prompt()
        est_in = estimate_tokens(system + "".join(m.content + json.dumps([c.arguments for c in m.tool_calls])
                                                  for m in messages))
        if tools:
            est_in += estimate_tokens(json.dumps([t.parameters for t in tools]))
        if est_in > int(self.limits["max_input_tokens"]):
            raise LimitExceeded("input_tokens_exceeded", f"~{est_in} input tokens > {self.limits['max_input_tokens']}")
        max_out = int(min(max_tokens or self.limits["max_output_tokens"], self.limits["max_output_tokens"],
                          int(self.defn.model.params.get("max_output_tokens", 10**9))))
        candidates = [self.defn.model.catalog_id, *self.defn.model.fallbacks]
        last_err: Exception | None = None
        params = {k: v for k, v in self.defn.model.params.items() if k != "max_output_tokens"}
        daily = self.policy.get("daily_budget_usd")
        key = bu_day_key(self.owner_bu)
        for mid in candidates:
            entry = self.catalog["models"].get(mid)
            if entry is None:
                self.warn(f"model {mid} missing from bundle")
                continue
            if entry.get("status") in UNUSABLE_STATUSES:
                self.warn(f"model {mid} is {entry['status']}; trying fallback")
                continue
            # token estimates are approximate; reserve against a 25% margin on input
            in_cost = cost_usd(entry, int(est_in * 1.25), 0)
            if in_cost > self.remaining_usd:
                raise BudgetExceeded(f"prompt alone (~${in_cost:.4f}) exceeds remaining budget ${self.remaining_usd:.4f}")
            # shrink max output so the worst case fits both the run budget and the BU's daily headroom
            headroom = min(self.remaining_usd, await self.svc.budgets.available(key, daily))
            if in_cost > headroom:
                raise BudgetExceeded(f"daily budget for {self.owner_bu} exhausted")
            per_out = cost_usd(entry, 0, 1_000_000) / 1_000_000
            if per_out > 0:
                max_out = max(1, min(max_out, int((headroom - in_cost) / per_out)))
            reserve = min(headroom, in_cost + cost_usd(entry, 0, max_out))
            try:
                await self.svc.budgets.reserve(key, reserve, daily)
            except BudgetExhausted as e:
                raise BudgetExceeded(str(e)) from e
            req = ModelRequest(system=system, messages=messages, tools=tools or [],
                               json_schema=self.output_schema() if json_mode else None,
                               max_output_tokens=max_out, params=params)
            t0 = time.time()
            resp: ModelResponse | None = None
            for attempt in range(3):
                try:
                    resp = await asyncio.wait_for(self.svc.models.generate(entry, req),
                                                  timeout=max(1.0, self.deadline - time.time()))
                    break
                except ModelError as e:
                    last_err = e
                    if not e.retryable or attempt == 2:
                        break
                    await asyncio.sleep(min(2 ** attempt * 0.5, max(0.0, self.deadline - time.time())))
                except asyncio.TimeoutError as e:
                    await self.svc.budgets.settle(key, reserve, 0.0)
                    raise LimitExceeded("latency_exceeded", "model call exceeded the deadline") from e
            if resp is None:
                await self.svc.budgets.settle(key, reserve, 0.0)
                await self.step("model", mid, "error", t0, error=str(last_err))
                self.warn(f"model {mid} failed: {last_err}")
                continue
            actual = cost_usd(entry, resp.input_tokens, resp.output_tokens)
            await self.svc.budgets.settle(key, reserve, actual)
            self.cost.add(Cost(usd=actual, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, model_calls=1))
            await self.step("model", mid, "ok" if resp.stop_reason != "refusal" else "refused", t0,
                            input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd=round(actual, 6),
                            stop_reason=resp.stop_reason, tool_calls=[c.name for c in resp.tool_calls])
            if resp.stop_reason == "refusal":
                raise Refused(f"model {mid} declined the request")
            if resp.stop_reason == "max_tokens":
                self.warn("model output hit max_output_tokens")
            if resp.text:
                self.partial = resp.text
            return resp
        raise ModelError(f"no usable model: {last_err or 'all candidates unavailable'}")

    # ---------------------------------------------------------------- tools/agents

    async def toolset(self) -> tuple[list[ToolSpec], dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]]:
        if self._toolset is not None:
            return self._toolset
        specs: list[ToolSpec] = []
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}
        for b in self.defn.bindings.tools:
            entry = self.catalog["tools"].get(b.ref)
            if entry is None:
                self.warn(f"tool {b.ref} missing from bundle")
                continue
            binding = b.model_dump()
            spec = tool_spec(binding, entry)
            specs.append(spec)

            async def run_tool(args: dict[str, Any], _entry=entry, _params=b.params) -> Any:
                return await self.svc.tools.run(_entry, {**_params, **args}, self.ctx)

            handlers[spec.name] = run_tool
        for ab in self.defn.bindings.agents:
            name = "agent__" + re.sub(r"[^A-Za-z0-9_-]", "_", ab.name)
            try:
                bundle = await self.svc.resolver.resolve(ab.name, ab.alias)
                d = bundle["primary"]["definition"]
                schema = d.get("contract", {}).get("input_schema") or {"type": "object"}
                desc = ab.description or d.get("description") or d.get("contract", {}).get("description", "")
            except Exception as e:
                self.warn(f"agent {ab.name}@{ab.alias} unavailable: {e}")
                continue
            specs.append(ToolSpec(name=name, description=f"Call agent {ab.name}: {desc}", parameters=schema))

            async def run_agent(args: dict[str, Any], _b=ab) -> Any:
                resp = await self.call_agent(_b, args)
                if resp.status == "failed":
                    raise ToolError(f"agent {_b.name} failed: {resp.error.message if resp.error else 'unknown'}")
                return {"status": resp.status, "output": resp.output,
                        "sources": [s.model_dump(include={"id", "title", "uri"}) for s in resp.sources]}

            handlers[name] = run_agent
        self._toolset = (specs, handlers)
        return self._toolset

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.check_limits()
        self.tool_calls += 1
        if self.tool_calls > int(self.limits["max_tool_calls"]):
            raise LimitExceeded("tool_calls_exceeded", f"max_tool_calls {self.limits['max_tool_calls']} exceeded")
        t0 = time.time()
        _, handlers = await self.toolset()
        try:
            self.enforcer.whole("tool_call", {"tool": name, "arguments": args}, {"tool_name": name})
            args = self.enforcer.value("tool_call", args)
        except GuardrailBlocked as e:
            await self.step("guardrail", e.finding.guardrail, "blocked", t0, stage="tool_call", tool=name)
            return f"Tool call blocked by guardrail: {e.finding.detail}", True
        handler = handlers.get(name)
        if handler is None:
            return f"Unknown tool {name}", True
        self.cost.add(Cost(tool_calls=1))
        try:
            result = await handler(args)
            is_error = False
        except (ToolError, CallContextError) as e:
            result, is_error = f"{type(e).__name__}: {e}", True
        except Exception as e:  # tool bugs must not kill the run
            log.exception("tool %s failed", name)
            result, is_error = f"tool error: {e}", True
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        out = self.enforcer.text("tool_result", text, raise_on_block=False)
        if out.blocked:
            self.warn(f"tool {name} result blocked by guardrail")
            text, is_error = "Tool result withheld by guardrail.", True
        else:
            text = out.text
        await self.step("tool", name, "error" if is_error else "ok", t0, arguments=args, result_preview=text[:300])
        return text[:50_000], is_error

    async def call_agent(self, binding: AgentBinding, child_input: dict[str, Any], *,
                         budget_share: float | None = None) -> InvokeResponse:
        t0 = time.time()
        bundle = await self.svc.resolver.resolve(binding.name, binding.alias)
        rel = pick_release(bundle, self.ctx.trace_id)
        d = rel["definition"]
        if not d.get("exposure", {}).get("callable_by_agents", True):
            raise ToolError(f"agent {binding.name} is not callable by agents")
        budget = min(self.remaining_usd if budget_share is None else budget_share, float(rel["limits"]["max_cost_usd"]))
        child_ctx = self.ctx.child(binding.name, budget, max_depth=int(self.limits["max_depth"]))
        child = Execution(self.svc, rel, bundle["policy"], child_ctx, agent=binding.name, alias=binding.alias,
                          owner_bu=bundle.get("owner_bu"), emitter=self.emitter, mode="child")
        resp = await child.run(child_input)
        self.cost.add(resp.cost)
        for s in resp.sources:
            if all(s.id != x.id for x in self.sources):
                self.sources.append(s)
        await self.step("agent", f"{binding.name}@{rel['version']}", resp.status, t0, run_id=resp.run_id,
                        cost_usd=round(resp.cost.usd, 6), depth=child_ctx.depth)
        try:
            await self.svc.runs.record_response(
                resp, agent=binding.name, alias=binding.alias, mode="child", input=child_input,
                principal=self.ctx.caller, on_behalf_of=self.ctx.on_behalf_of, bu=bundle.get("owner_bu"),
                depth=child_ctx.depth)
        except Exception:
            log.exception("recording child run failed")
        return resp

    # ------------------------------------------------------------------------ run

    async def finalize(self, out: Any) -> Any:
        if self.wants_json:
            value = out
            if isinstance(out, str):
                try:
                    value = parse_json_text(out)
                except (json.JSONDecodeError, ValueError):
                    value = await self._repair(out, "not valid JSON")
            schema = self.output_schema()
            if schema:
                try:
                    jsonschema.validate(value, schema)
                except jsonschema.ValidationError as e:
                    value = await self._repair(json.dumps(value), str(e).splitlines()[0])
                    try:
                        jsonschema.validate(value, schema)
                    except jsonschema.ValidationError as e2:
                        raise OutputInvalid(f"output does not match schema: {str(e2).splitlines()[0]}") from e2
        else:
            value = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2, default=str)
            disclaimer = self.fctx.guardrails.disclaimer.strip() if self.fctx else ""
            if disclaimer and disclaimer not in value:
                value = f"{value.rstrip()}\n\n_{disclaimer}_"
        value = self.enforcer.value("output", value)
        self.enforcer.whole("output", value, {"output_schema": self.output_schema(),
                                              "sources": [s.model_dump() for s in self.sources]})
        if self.defn.output.require_citations and not self.sources:
            self.warn("citations required but no sources were used")
            self.force_partial = True
        return value

    async def _repair(self, text: str, problem: str) -> Any:
        if self.remaining_usd <= 0:
            raise OutputInvalid(f"invalid output ({problem}) and no budget to repair")
        resp = await self.model(
            [Message("user", f"Your previous answer was invalid ({problem}). Previous answer:\n{text[:8000]}\n\n"
                             "Return only the corrected JSON.")],
            json_mode=True,
        )
        try:
            return parse_json_text(resp.text)
        except (json.JSONDecodeError, ValueError) as e:
            raise OutputInvalid(f"output is not valid JSON after repair: {e}") from e

    async def run(self, input: dict[str, Any]) -> InvokeResponse:
        from .patterns import PATTERNS

        status, output, error, needs = "ok", None, None, None
        try:
            schema = self.defn.contract.input_schema
            if schema:
                try:
                    jsonschema.validate(input, schema)
                except jsonschema.ValidationError as e:
                    raise InvalidInput(str(e).splitlines()[0]) from e
            safe_input = self.enforcer.value("input", input)
            if safe_input != input:
                await self.step("guardrail", "input", "redacted", stage="input")
            out = await PATTERNS[self.defn.pattern.type](self, safe_input)
            output = await self.finalize(out)
            if self.force_partial:
                status = "partial"
        except PolicyResponse as p:
            # functional guardrail answered: a normal reply for the user, flagged for operators
            output = p.message
            self.flags.append("escalated" if p.kind == "escalation" else f"responded:{p.kind}")
            await self.step("guardrail", p.kind, "responded", stage="input", detail=p.detail)
        except NeedsInputSignal as s:
            status, self.state = "needs_input", s.state
            needs = NeedsInput(prompt=s.prompt, draft=s.draft)
            output = s.draft
        except GuardrailBlocked as e:
            status, error = "failed", ErrorInfo(code="guardrail_blocked", message=str(e))
            await self.step("guardrail", e.finding.guardrail, "blocked", stage=e.finding.stage, detail=e.finding.detail)
        except BudgetExceeded as e:
            status, error, output = "partial", ErrorInfo(code="budget_exceeded", message=str(e)), self.partial
        except LimitExceeded as e:
            status, error, output = "partial", ErrorInfo(code=e.code, message=str(e)), self.partial
        except CallContextError as e:
            status, error = "failed", ErrorInfo(code=e.code, message=str(e))
        except InvalidInput as e:
            status, error = "failed", ErrorInfo(code="invalid_input", message=str(e))
        except OutputInvalid as e:
            status, error = "failed", ErrorInfo(code="invalid_output", message=str(e))
        except Refused as e:
            status, error = "failed", ErrorInfo(code="model_refused", message=str(e))
        except ModelError as e:
            status, error = "failed", ErrorInfo(code="model_error", message=str(e))
        except Exception as e:
            log.exception("run %s failed", self.ctx.run_id)
            status, error = "failed", ErrorInfo(code="internal_error", message=f"{type(e).__name__}: {e}")
        if status == "partial" and output is not None and not isinstance(output, (str, dict, list)):
            output = str(output)
        if status == "partial" and isinstance(output, str):
            try:
                output = self.enforcer.value("output", output)
            except GuardrailBlocked:
                output = None
        for f in self.enforcer.findings:
            if f.action in ("warn", "redact"):
                self.warn(f"guardrail {f.guardrail}@{f.stage}: {f.detail}")
        resp = InvokeResponse(
            run_id=self.ctx.run_id,
            status=status,  # type: ignore[arg-type]
            output=output,
            sources=self.sources,
            cost=Cost(**{k: (round(v, 8) if isinstance(v, float) else v) for k, v in self.cost.model_dump().items()}),
            latency_ms=int((time.time() - self.started) * 1000),
            trace=Trace(trace_id=self.ctx.trace_id, run_id=self.ctx.run_id, parent_run_id=self.ctx.parent_run_id,
                        agent=self.agent, version=self.release.get("version", "?"), alias=self.alias,
                        content_hash=self.release.get("content_hash"), depth=self.ctx.depth, steps=self.steps),
            error=error,
            needs_input=needs,
            warnings=list(dict.fromkeys(self.warnings)),
            context=self.fctx.id if self.fctx else None,
            flags=self.flags,
        )
        if self.emitter and self.mode != "child":
            await self.emitter("result", resp.model_dump(mode="json"))
        return resp


def root_context(*, principal: str, roles: list[str], limits: dict[str, Any], agent: str,
                 budget_usd: float | None = None, trace_id: str | None = None,
                 run_id: str | None = None, attributes: dict[str, Any] | None = None) -> CallContext:
    budget = float(limits["max_cost_usd"]) if budget_usd is None else min(budget_usd, float(limits["max_cost_usd"]))
    ctx = CallContext.root(on_behalf_of=principal, roles=roles, budget_usd=budget,
                           max_depth=int(limits["max_depth"]), trace_id=trace_id, attributes=attributes)
    if run_id:
        ctx = replace(ctx, run_id=run_id)
    return ctx.enter(agent)


__all__ = ["Execution", "Services", "Resume", "root_context", "NeedsInputSignal", "InvalidInput"]
