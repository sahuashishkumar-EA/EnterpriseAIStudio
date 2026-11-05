"""Guardrail enforcer (hard controls in code), applied by stage:

  input        the caller's input, before anything else
  retrieval    each retrieved knowledge chunk, before it reaches the model
  tool_call    tool/agent name + arguments chosen by the model, before execution
  tool_result  what a tool/agent returned, before it reaches the model
  output       the final output, before it is returned

Actions: block (stop / drop), redact (replace matches), warn (record and continue).
The effective guardrail list comes from the resolve bundle: platform + BU required
guardrails merged with the definition's own (they cannot be removed or weakened).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema

PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "ip_address": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "nl_bsn": re.compile(r"\b\d{9}\b"),
}
DEFAULT_PII = ["email", "phone", "iban", "credit_card"]

INJECTION_PATTERNS = [
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|rules)",
    r"disregard (?:all |any )?(?:the )?(?:previous|prior|above|system) (?:instructions|prompts?)",
    r"(?:reveal|print|show|repeat|output) (?:your |the )?(?:system prompt|hidden instructions|instructions above)",
    r"you are now (?:in )?(?:developer|dan|jailbreak|god) mode",
    r"(?:new|updated) system (?:prompt|instructions)\s*:",
    r"</?(?:system|assistant)>",
    r"(?:send|post|exfiltrate|upload) (?:all |the )?(?:data|credentials|secrets|api keys?) to",
]


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    s = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        s += n
    return s % 10 == 0


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    r = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in r)
    return int(num) % 97 == 1


def _bsn_ok(s: str) -> bool:
    if len(s) != 9:
        return False
    total = sum(int(c) * w for c, w in zip(s[:8], range(9, 1, -1))) - int(s[8])
    return total % 11 == 0


VALIDATORS = {"credit_card": _luhn, "iban": _iban_ok, "nl_bsn": _bsn_ok}


@dataclass
class Finding:
    guardrail: str
    stage: str
    action: str
    detail: str


@dataclass
class Outcome:
    text: str
    blocked: bool = False
    findings: list[Finding] = field(default_factory=list)


class PolicyResponse(Exception):
    """A functional guardrail answered on the agent's behalf (refusal, hand-off, privacy note)."""

    def __init__(self, message: str, kind: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.detail = detail


class GuardrailBlocked(Exception):
    def __init__(self, finding: Finding):
        super().__init__(f"{finding.guardrail} blocked at {finding.stage}: {finding.detail}")
        self.finding = finding


def _pii(text: str, params: dict[str, Any]) -> list[tuple[str, int, int]]:
    hits: list[tuple[str, int, int]] = []
    for ent in params.get("entities") or DEFAULT_PII:
        pat = PII_PATTERNS.get(ent)
        if not pat:
            continue
        for m in pat.finditer(text):
            v = VALIDATORS.get(ent)
            if v and not v(m.group(0).replace(" ", "").replace("-", "")):
                continue
            if ent == "phone" and len(re.sub(r"\D", "", m.group(0))) < 9:
                continue
            hits.append((ent, m.start(), m.end()))
    # drop overlaps (keep earliest/longest)
    hits.sort(key=lambda x: (x[1], -(x[2] - x[1])))
    out, last = [], -1
    for h in hits:
        if h[1] >= last:
            out.append(h)
            last = h[2]
    return out


def _redact(text: str, spans: list[tuple[str, int, int]]) -> str:
    for ent, s, e in sorted(spans, key=lambda x: -x[1]):
        text = text[:s] + f"[REDACTED:{ent}]" + text[e:]
    return text


def check_text(guardrail: dict[str, Any], text: str, ctx: dict[str, Any] | None = None) -> Outcome:
    """Apply one guardrail to one text value."""
    t, stage, action, params = guardrail["type"], guardrail["stage"], guardrail.get("action", "block"), guardrail.get("params") or {}
    ctx = ctx or {}
    out = Outcome(text=text)

    def hit(detail: str) -> None:
        out.findings.append(Finding(t, stage, action, detail))
        if action in ("block", "respond"):
            out.blocked = True

    if t == "pii_redaction":
        spans = _pii(text, params)
        if spans:
            kinds = sorted({s[0] for s in spans})
            if action == "redact":
                out.text = _redact(text, spans)
                out.findings.append(Finding(t, stage, action, f"redacted {len(spans)} ({', '.join(kinds)})"))
            else:
                hit(f"PII detected: {', '.join(kinds)}")
    elif t == "prompt_injection":
        pats = INJECTION_PATTERNS + list(params.get("extra_patterns") or [])
        for p in pats:
            m = re.search(p, text, flags=re.I)
            if m:
                if action == "redact":
                    out.text = re.sub(p, "[REMOVED]", out.text, flags=re.I)
                    out.findings.append(Finding(t, stage, action, f"removed injection phrase {m.group(0)!r}"))
                else:
                    hit(f"possible prompt injection: {m.group(0)!r}")
                    break
    elif t in ("topic_policy", "escalation"):
        for term in params.get("terms") or []:
            if term.strip() and re.search(rf"\b{re.escape(term.strip())}\b", out.text, re.I):
                hit(f"matched {term!r}")
                break
    elif t == "topic_denylist":
        for term in params.get("terms") or []:
            pat = re.compile(rf"\b{re.escape(term)}\b", re.I)
            if pat.search(out.text):
                if action == "redact":
                    out.text = pat.sub("[REDACTED]", out.text)
                    out.findings.append(Finding(t, stage, action, f"redacted term {term!r}"))
                else:
                    hit(f"denied topic {term!r}")
                    break
    elif t == "regex_block":
        for p in params.get("patterns") or []:
            if re.search(p, out.text):
                if action == "redact":
                    out.text = re.sub(p, "[REDACTED]", out.text)
                    out.findings.append(Finding(t, stage, action, f"redacted /{p}/"))
                else:
                    hit(f"matched /{p}/")
                    break
    elif t == "max_length":
        mx = int(params["max_chars"])
        if len(out.text) > mx:
            if action == "redact":
                out.text = out.text[:mx]
                out.findings.append(Finding(t, stage, action, f"truncated to {mx} chars"))
            else:
                hit(f"length {len(out.text)} > {mx}")
    elif t == "json_schema":
        schema = params.get("schema") or ctx.get("output_schema")
        if schema:
            try:
                jsonschema.validate(json.loads(out.text), schema)
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                hit(f"output does not match schema: {str(e).splitlines()[0][:200]}")
    elif t == "citation_required":
        need = int(params.get("min", 1))
        cited = set(re.findall(r"\[(\d+)\]", out.text))
        have = max(len(cited), len(ctx.get("sources") or []) if not cited else 0)
        if len(cited) < need and have < need:
            hit(f"{len(cited)} citation(s), need {need}")
    elif t == "tool_allowlist":
        name = ctx.get("tool_name", "")
        if name not in (params.get("tools") or []):
            hit(f"tool {name!r} not in allow-list")
    return out


WHOLE_TYPES = {"json_schema", "citation_required", "tool_allowlist"}


class Enforcer:
    def __init__(self, guardrails: list[dict[str, Any]]):
        self.by_stage: dict[str, list[dict[str, Any]]] = {}
        for g in guardrails:
            self.by_stage.setdefault(g["stage"], []).append(g)
        self.findings: list[Finding] = []

    def has(self, stage: str) -> bool:
        return bool(self.by_stage.get(stage))

    def text(self, stage: str, text: str, ctx: dict[str, Any] | None = None, *, raise_on_block: bool = True) -> Outcome:
        result = Outcome(text=text)
        for g in self.by_stage.get(stage, []):
            if g["type"] in WHOLE_TYPES:
                continue
            o = check_text(g, result.text, ctx)
            result.text = o.text
            result.findings += o.findings
            self.findings += o.findings
            if o.blocked:
                result.blocked = True
                if g.get("action") == "respond":
                    f = o.findings[-1]
                    msg = (g.get("params") or {}).get("message") or "I can't help with that here."
                    raise PolicyResponse(msg, g["type"], f.detail)
                if raise_on_block:
                    raise GuardrailBlocked(next(f for f in o.findings if f.action == "block"))
                break
        return result

    def value(self, stage: str, value: Any, ctx: dict[str, Any] | None = None) -> Any:
        """Apply text guardrails to every string inside a JSON-like value."""
        if not any(g["type"] not in WHOLE_TYPES for g in self.by_stage.get(stage, [])):
            return value
        if isinstance(value, str):
            return self.text(stage, value, ctx).text
        if isinstance(value, dict):
            return {k: self.value(stage, v, ctx) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(stage, v, ctx) for v in value]
        return value

    def whole(self, stage: str, value: Any, ctx: dict[str, Any] | None = None) -> None:
        """Checks that need the whole serialized value (json_schema, citations, tool_allowlist)."""
        gs = [g for g in self.by_stage.get(stage, []) if g["type"] in WHOLE_TYPES]
        if not gs:
            return
        text = value if isinstance(value, str) else json.dumps(value)
        for g in gs:
            o = check_text(g, text, ctx)
            self.findings += o.findings
            if o.blocked:
                raise GuardrailBlocked(next(f for f in o.findings if f.action == "block"))
