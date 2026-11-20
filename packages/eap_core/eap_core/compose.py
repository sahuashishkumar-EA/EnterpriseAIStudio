"""Compose what the model actually receives from the two ownership layers of an agent:

  technical layer  (technical expert)   prompt.system / instructions / context items,
                                        model, tools, technical guardrails, limits, output
  functional layer (functional expert)  one FunctionalContext: role, goal, behaviour, always /
                                        never, notes, web sources, functional guardrails,
                                        examples, starters
  platform layer   (policy)             soft controls appended last, so they win

Used by the runtime at execution time and by the governor for the "what the model sees"
preview, so both always agree.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .schema import AgentDefinition, FunctionalContext

TONE = {
    "friendly": "warm, approachable and positive",
    "professional": "clear, competent and courteous",
    "formal": "formal and precise; avoid slang and contractions",
    "empathetic": "patient and empathetic; acknowledge the person's situation before helping",
    "playful": "light-hearted and upbeat, while staying helpful",
    "neutral": "neutral and factual",
}
VERBOSITY = {
    "brief": "Keep answers short: one to three sentences unless more is truly needed.",
    "balanced": "Give complete but compact answers.",
    "detailed": "Give thorough answers with explanations and relevant detail.",
}
FORMAT = {
    "plain": "Write in plain prose.",
    "bullets": "Prefer bullet points for lists of facts or options.",
    "steps": "When explaining how to do something, use numbered steps.",
    "auto": "",
}

DEFAULT_PII_MESSAGE = "For your privacy, please don't share personal details such as card numbers, IBANs or ID numbers here."


def web_source_id(agent: str, context_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", f"web-{agent}-{context_id}")[:63]


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i.strip()}" for i in items if i.strip())


def functional_section(ctx: FunctionalContext) -> str:
    b = ctx.behavior
    parts: list[str] = []
    who = []
    if ctx.role:
        who.append(f"You are {ctx.role.strip().rstrip('.')}.")
    if ctx.goal:
        who.append(f"Your goal: {ctx.goal.strip()}")
    if ctx.audience:
        who.append(f"You are talking to: {ctx.audience.strip()}")
    if who:
        parts.append("## Role\n" + "\n".join(who))
    style = [f"Tone: {TONE.get(b.tone, b.tone)}.", VERBOSITY[b.verbosity]]
    if FORMAT.get(b.format):
        style.append(FORMAT[b.format])
    style.append(f"Language: {b.language}." if b.language else "")
    style.append("You may use emoji sparingly." if b.emoji else "Do not use emoji.")
    parts.append("## Behaviour\n" + " ".join(x for x in style if x))
    if ctx.always:
        parts.append("## Always\n" + _bullets(ctx.always))
    if ctx.never:
        parts.append("## Never\n" + _bullets(ctx.never))
    g = ctx.guardrails
    if g.allowed_topics:
        parts.append("## Scope\nOnly help with these topics: " + ", ".join(g.allowed_topics)
                     + f". For anything else, reply: \"{g.refusal_message}\"")
    if g.escalation.triggers:
        parts.append("## Escalation\nIf the person asks for a human, is upset, or raises: "
                     + ", ".join(g.escalation.triggers) + f" -> reply: \"{g.escalation.message}\"")
    if ctx.notes:
        parts.append("## Facts you can rely on\n" + "\n\n".join(f"### {n.title}\n{n.text.strip()}" for n in ctx.notes))
    if ctx.examples:
        ex = "\n\n".join(f"User: {e.user.strip()}\nAssistant: {e.assistant.strip()}" for e in ctx.examples)
        parts.append("## Example exchanges (follow the style, not the exact words)\n" + ex)
    return "\n\n".join(parts)


def system_prompt(
    defn: AgentDefinition,
    ctx: FunctionalContext | None,
    soft_controls: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> str:
    p = defn.prompt
    parts = ["# System context", p.system.strip()]
    if p.instructions:
        parts.append(p.instructions.strip())
    for c in p.context:
        parts.append(f"## {c.title}\n{c.text.strip()}")
    if p.examples:
        parts.append("## Examples\n" + "\n".join(json.dumps(e, ensure_ascii=False) for e in p.examples))
    if ctx is not None:
        parts.append(f"# Functional context: {ctx.name}")
        parts.append(functional_section(ctx))
    if soft_controls:
        parts.append("# Platform rules (always apply)\n" + _bullets(soft_controls))
    if defn.output.format == "json" and output_schema:
        parts.append("# Output\nRespond with a single JSON value matching this JSON schema, and nothing else:\n"
                     + json.dumps(output_schema))
    elif defn.output.format == "markdown":
        parts.append("# Output\nRespond in Markdown.")
    return "\n\n".join(x for x in parts if x and x.strip())


def functional_guardrails(ctx: FunctionalContext | None) -> list[dict[str, Any]]:
    """Hard, code-enforced guardrails derived from the functional context. They answer with a
    friendly message (action 'respond') instead of failing the run."""
    if ctx is None:
        return []
    g = ctx.guardrails
    out: list[dict[str, Any]] = []
    if g.blocked_topics:
        out.append({"type": "topic_policy", "stage": "input", "action": "respond",
                    "params": {"terms": g.blocked_topics, "message": g.refusal_message}})
    if g.escalation.triggers:
        out.append({"type": "escalation", "stage": "input", "action": "respond",
                    "params": {"terms": g.escalation.triggers, "message": g.escalation.message}})
    if g.personal_data == "redact":
        ents = ["email", "phone", "iban", "credit_card"]
        out.append({"type": "pii_redaction", "stage": "input", "action": "redact", "params": {"entities": ents}})
        out.append({"type": "pii_redaction", "stage": "output", "action": "redact", "params": {"entities": ents}})
    elif g.personal_data == "refuse":
        out.append({"type": "pii_redaction", "stage": "input", "action": "respond",
                    "params": {"entities": ["iban", "credit_card", "nl_bsn"], "message": DEFAULT_PII_MESSAGE}})
    return out


DRAFT_VERSIONS = (None, "", "draft", "test", "judge")


def versioned_source_id(agent: str, context_id: str, version: str | None) -> str:
    """Scans write the draft index; publishing snapshots it per version, so re-scanning a
    draft never changes what a live version answers from."""
    base = web_source_id(agent, context_id)
    if version in DRAFT_VERSIONS:
        return base
    suffix = "--v" + re.sub(r"[^0-9a-z]", "-", str(version).lower())
    return base[: 63 - len(suffix)] + suffix


def knowledge_refs(defn: AgentDefinition, ctx: FunctionalContext | None, version: str | None = None) -> list[str]:
    """Knowledge sources to ground answers in: the context's scanned web pages (for this version)."""
    if ctx is None or not ctx.web_sources:
        return []
    return [versioned_source_id(defn.name, ctx.id, version)]


def completeness(ctx: FunctionalContext) -> dict[str, Any]:
    """Checklist shown in the functional studio."""
    checks = {
        "role": bool(ctx.role.strip()),
        "goal": bool(ctx.goal.strip()),
        "audience": bool(ctx.audience.strip()),
        "always / never": bool(ctx.always or ctx.never),
        "knowledge (web or notes)": bool(ctx.web_sources or ctx.notes),
        "boundaries": bool(ctx.guardrails.blocked_topics or ctx.guardrails.allowed_topics),
        "escalation": bool(ctx.guardrails.escalation.triggers),
        "examples": bool(ctx.examples),
        "starters": bool(ctx.starters),
    }
    from .schema import FunctionalGuardrails

    defaults = FunctionalGuardrails()
    g = ctx.guardrails
    hints: list[str] = []
    if g.blocked_topics and g.refusal_message == defaults.refusal_message:
        hints.append("The refusal reply is the generic default — say what the agent CAN help with instead.")
    if g.escalation.triggers and g.escalation.message == defaults.escalation.message:
        hints.append("The hand-over reply is generic — tell people how and when they will be contacted.")
    if ctx.role and len(ctx.role.split()) < 4:
        hints.append("The role is very short — add who the agent works for and its expertise.")
    if not ctx.examples and ctx.behavior.tone in ("formal", "playful", "empathetic"):
        hints.append(f"A {ctx.behavior.tone} tone is easier to get right with one or two example answers.")
    if ctx.web_sources and not ctx.notes:
        hints.append("Consider a few facts in Facts & FAQ for questions the web pages don't answer well.")
    if len(ctx.starters) == 1:
        hints.append("Add a second or third starter — starters also become the automatic smoke tests.")
    if g.personal_data == "allow" and ctx.audience and "customer" in ctx.audience.lower():
        hints.append("Personal data is allowed for a customer-facing audience — consider 'Redact it'.")
    return {"score": round(100 * sum(checks.values()) / len(checks)), "checks": checks, "hints": hints}
