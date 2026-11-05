"""Orchestration patterns. Each takes the Execution and the (guarded) input and returns a
raw output (text or JSON value) that the executor then finalizes (format, schema,
output guardrails)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from eap_core.contracts import Source

from .executor import Execution, InvalidInput, NeedsInputSignal, get_path, parse_json_text
from .models import Message


async def single_shot(ex: Execution, inp: dict[str, Any]) -> Any:
    resp = await ex.model([Message("user", ex.render_input(inp))], json_mode=ex.wants_json)
    return resp.text


async def rag(ex: Execution, inp: dict[str, Any]) -> Any:
    params = ex.defn.pattern.params
    q = get_path(inp, params.get("query_path")) if params.get("query_path") else None
    if q is None:
        q = inp.get("query") or inp.get("question") or inp.get("text") or json.dumps(inp, ensure_ascii=False)
    query = q if isinstance(q, str) else json.dumps(q, ensure_ascii=False)
    t0 = time.time()
    chunks: list[tuple[dict[str, Any], str, str]] = []  # (row, text, knowledge ref)
    for kb in ex.defn.bindings.knowledge:
        src = ex.catalog["knowledge"].get(kb.ref)
        if src is None:
            ex.warn(f"knowledge {kb.ref} missing from bundle")
            continue
        try:
            hits = await ex.svc.knowledge.search(src, query, kb.top_k, kb.min_score)
        except ValueError as e:
            ex.warn(str(e))
            continue
        for h in hits:
            out = ex.enforcer.text("retrieval", h["content"], raise_on_block=False)
            if out.blocked:
                ex.warn(f"retrieved chunk {h['id']} dropped by guardrail")
                continue
            chunks.append((h, out.text, kb.ref))
    chunks.sort(key=lambda c: -c[0]["score"])
    budget = int(params.get("max_context_chars", 12000))
    lines, used = [], 0
    numbered: list[Source] = []
    for row, text, ref in chunks:
        if used + len(text) > budget and numbered:
            break
        n = len(numbered) + 1
        numbered.append(Source(id=row["id"], title=row.get("title", ""), uri=row.get("uri", ""),
                               snippet=text[:300], score=round(float(row["score"]), 4), knowledge_ref=ref))
        lines.append(f"[{n}] {row.get('title') or row.get('doc_id')}\n{text}")
        used += len(text)
    await ex.step("retrieval", ",".join(k.ref for k in ex.defn.bindings.knowledge), "ok", t0,
                  query=query[:200], hits=len(numbered))
    if not numbered:
        ex.warn("no relevant sources found")
    prompt = (f"{ex.render_input(inp)}\n\nSources:\n" + ("\n\n".join(lines) if lines else "(none found)") +
              "\n\nAnswer using only the sources above. Cite every claim with its source number like [1]. "
              "If the sources do not contain the answer, say so.")
    resp = await ex.model([Message("user", prompt)], json_mode=ex.wants_json)
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", resp.text)}
    used_sources = [s for i, s in enumerate(numbered, 1) if i in cited] or numbered
    for s in used_sources:
        if all(s.id != x.id for x in ex.sources):
            ex.sources.append(s)
    return resp.text


async def research_loop(ex: Execution, inp: dict[str, Any]) -> Any:
    specs, _ = await ex.toolset()
    params = ex.defn.pattern.params
    max_iter = min(int(params.get("max_iterations", 6)), int(ex.limits["max_iterations"]))
    parallel = params.get("parallel_tool_calls", True)
    msgs = [Message("user", ex.render_input(inp) + "\n\nUse the available tools when they help, then give the final answer.")]
    for _ in range(max_iter):
        resp = await ex.model(msgs, tools=specs, json_mode=ex.wants_json)
        if not resp.tool_calls:
            return resp.text
        msgs.append(Message("assistant", resp.text, tool_calls=resp.tool_calls))
        if parallel:
            results = await asyncio.gather(*(ex.call_tool(c.name, c.arguments) for c in resp.tool_calls))
        else:
            results = [await ex.call_tool(c.name, c.arguments) for c in resp.tool_calls]
        for c, (content, is_error) in zip(resp.tool_calls, results):
            msgs.append(Message("tool", content, tool_call_id=c.id, name=c.name, is_error=is_error))
    ex.warn(f"iteration limit {max_iter} reached")
    msgs.append(Message("user", "The tool-use limit has been reached. Give your best final answer now."))
    resp = await ex.model(msgs, json_mode=ex.wants_json)
    return resp.text


async def map_reduce(ex: Execution, inp: dict[str, Any]) -> Any:
    p = ex.defn.pattern.params
    items = get_path(inp, p["items_path"])
    if not isinstance(items, list):
        raise InvalidInput(f"{p['items_path']} must be a list")
    max_items = int(p.get("max_items", 50))
    if len(items) > max_items:
        ex.warn(f"only the first {max_items} of {len(items)} items were processed")
        items = items[:max_items]
    sem = asyncio.Semaphore(int(p.get("concurrency", 4)))
    binding = None
    if p.get("map_agent"):
        binding = next((b for b in ex.defn.bindings.agents if b.name == p["map_agent"]), None)
        if binding is None:
            raise InvalidInput(f"map_agent {p['map_agent']} is not bound")
    n = max(1, len(items))

    async def one(i: int, item: Any) -> Any:
        async with sem:
            if binding is not None:
                share = ex.remaining_usd / max(1, n - i) if ex.remaining_usd else 0.0
                resp = await ex.call_agent(binding, item if isinstance(item, dict) else {"item": item}, budget_share=share)
                if resp.status == "failed":
                    return {"error": resp.error.message if resp.error else "failed"}
                return resp.output
            prompt = (p.get("map_prompt") or "Process this item.") + "\n\nItem (JSON):\n" + json.dumps(item, ensure_ascii=False)
            return (await ex.model([Message("user", prompt)])).text

    mapped = await asyncio.gather(*(one(i, it) for i, it in enumerate(items)))
    if p.get("reduce_prompt"):
        prompt = (p["reduce_prompt"] + "\n\nOriginal input:\n" + json.dumps(inp, ensure_ascii=False, default=str)
                  + "\n\nMapped results (JSON):\n" + json.dumps(mapped, ensure_ascii=False, default=str))
        return (await ex.model([Message("user", prompt)], json_mode=ex.wants_json)).text
    return mapped


async def human_in_the_loop(ex: Execution, inp: dict[str, Any]) -> Any:
    p = ex.defn.pattern.params
    state = dict(ex.resume.state) if ex.resume else {"revisions": 0, "feedback": []}
    if ex.resume is not None:
        decision = ex.resume.input
        if decision.get("approved"):
            return state["draft"]
        state["feedback"] = [*state.get("feedback", []), str(decision.get("feedback", "")).strip()]
        state["revisions"] = int(state.get("revisions", 0)) + 1
        if state["revisions"] > int(p.get("max_revisions", 2)):
            ex.warn("maximum revisions reached; returning the last draft")
            ex.force_partial = True
            return state["draft"]
    prompt = ex.render_input(inp)
    if state.get("feedback"):
        prompt += "\n\nPrevious draft:\n" + (state["draft"] if isinstance(state.get("draft"), str)
                                             else json.dumps(state.get("draft")))
        prompt += "\n\nReviewer feedback to address:\n" + "\n".join(f"- {f}" for f in state["feedback"] if f)
    resp = await ex.model([Message("user", prompt)], json_mode=ex.wants_json)
    draft: Any = resp.text
    if ex.wants_json:
        try:
            draft = parse_json_text(resp.text)
        except (json.JSONDecodeError, ValueError):
            pass
    draft = ex.enforcer.value("output", draft)
    state["draft"] = draft
    raise NeedsInputSignal(p.get("review_prompt") or "Please review the draft. Approve, or give feedback.", draft, state)


PATTERNS = {
    "single_shot": single_shot,
    "rag": rag,
    "research_loop": research_loop,
    "map_reduce": map_reduce,
    "human_in_the_loop": human_in_the_loop,
}
