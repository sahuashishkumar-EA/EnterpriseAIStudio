"""Eval service: datasets (JSON / JSONL / CSV, written in the console or uploaded) and
assertions run against a draft or a published version via the runtime's
/internal/v1/execute endpoint. The Governor never calls models directly; even the
llm_judge assertion runs as a small judge agent on the runtime.

Case shape:  {"id": "...", "input": {...}, "expected": {...}, "assertions": [...]}
Suite-level assertions apply to every case; case assertions are added.

Assertion paths address the run result:
  status, output, output.<field>..., sources, cost.usd, latency_ms
Values may come from the case with value_from="expected.<field>".
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from typing import Any

from eap_core import plugins

ASSERTION_TYPES = {s.type for s in plugins.list_specs("assertion")}


class DatasetError(ValueError):
    pass


# ---------------------------------------------------------------------- datasets


def _maybe_json(v: str) -> Any:
    s = v.strip()
    if s[:1] in ("{", "[") or s in ("true", "false", "null"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return v
    return v


def _normalize_case(raw: Any, i: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DatasetError(f"case {i}: must be an object")
    if "input" not in raw:
        # bare row: everything except reserved keys is input
        reserved = {"id", "expected", "assertions", "context"}
        raw = {**{k: raw[k] for k in reserved if k in raw}, "input": {k: v for k, v in raw.items() if k not in reserved}}
    inp = raw["input"]
    if isinstance(inp, str):
        inp = {"text": inp}
    if not isinstance(inp, dict):
        raise DatasetError(f"case {i}: input must be an object or string")
    expected = raw.get("expected", {})
    if not isinstance(expected, dict):
        expected = {"value": expected}
    assertions = raw.get("assertions", []) or []
    for a in assertions:
        check_assertion_spec(a, f"case {i}")
    case = {"id": str(raw.get("id") or f"case-{i + 1}"), "input": inp, "expected": expected, "assertions": assertions}
    if raw.get("context"):
        case["context"] = str(raw["context"])  # functional context to evaluate under
    return case


def parse_dataset(content: str | bytes, fmt: str | None = None, filename: str | None = None) -> list[dict[str, Any]]:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    fmt = (fmt or "").lower()
    if not fmt and filename:
        fmt = filename.rsplit(".", 1)[-1].lower()
    if not fmt:
        s = content.lstrip()
        fmt = "json" if s.startswith("[") or s.startswith('{"cases"') else ("jsonl" if s.startswith("{") else "csv")
    rows: list[Any]
    if fmt == "json":
        data = json.loads(content)
        rows = data["cases"] if isinstance(data, dict) and "cases" in data else data
        if not isinstance(rows, list):
            raise DatasetError("JSON dataset must be a list of cases or {\"cases\": [...]}")
    elif fmt == "jsonl":
        rows = []
        for n, line in enumerate(content.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise DatasetError(f"line {n}: {e}") from e
    elif fmt == "csv":
        rows = []
        for rec in csv.DictReader(io.StringIO(content)):
            case: dict[str, Any] = {"input": {}, "expected": {}}
            for k, v in rec.items():
                if k is None:
                    continue
                k = k.strip()
                if k == "id":
                    case["id"] = v
                elif k == "input":
                    val = _maybe_json(v)
                    case["input"] = val if isinstance(val, dict) else {"text": val}
                elif k == "expected":
                    val = _maybe_json(v)
                    case["expected"] = val if isinstance(val, dict) else {"value": val}
                elif k == "context":
                    if v.strip():
                        case["context"] = v.strip()
                elif k == "assertions":
                    case["assertions"] = json.loads(v) if v.strip() else []
                elif k.startswith("expected."):
                    case["expected"][k[9:]] = _maybe_json(v)
                elif k.startswith("input."):
                    case["input"][k[6:]] = _maybe_json(v)
                else:
                    case["input"][k] = _maybe_json(v)
            rows.append(case)
    else:
        raise DatasetError(f"unsupported dataset format {fmt!r} (json, jsonl, csv)")
    if not rows:
        raise DatasetError("dataset is empty")
    return [_normalize_case(r, i) for i, r in enumerate(rows)]


def check_assertion_spec(a: Any, where: str = "suite") -> None:
    if not isinstance(a, dict) or "type" not in a:
        raise DatasetError(f"{where}: assertion must be an object with a type")
    if a["type"] not in ASSERTION_TYPES:
        raise DatasetError(f"{where}: unknown assertion type {a['type']!r}")
    params = {k: v for k, v in a.items() if k != "type"}
    errs = plugins.validate_params("assertion", a["type"], params)
    if errs:
        raise DatasetError(f"{where}: {a['type']}: {'; '.join(errs)}")


# -------------------------------------------------------------------- assertions

_MISSING = object()


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        elif isinstance(cur, str) and part in ("length", "len"):
            cur = len(cur)
        else:
            return _MISSING
    return cur


def _value(a: dict[str, Any], case: dict[str, Any]) -> Any:
    if a.get("value_from"):
        return get_path(case, a["value_from"])
    return a.get("value")


def _text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)


def check(a: dict[str, Any], result: dict[str, Any], case: dict[str, Any]) -> tuple[bool, str]:
    t = a["type"]
    if t == "status":
        return result.get("status") == a["equals"], f"status={result.get('status')}"
    if t == "max_cost_usd":
        c = (result.get("cost") or {}).get("usd", 0.0)
        return c <= a["max"], f"cost={c:.6f}"
    if t == "max_latency_ms":
        lat = result.get("latency_ms", 0)
        return lat <= a["max"], f"latency={lat}ms"
    if t == "citations":
        n = len(result.get("sources") or [])
        return n >= a.get("min", 1), f"sources={n}"
    actual = get_path(result, a["path"])
    if actual is _MISSING:
        return (t == "not_contains"), f"path {a['path']} missing"
    if t == "equals":
        exp = _value(a, case)
        return actual == exp, f"actual={_text(actual)[:200]}"
    if t in ("contains", "not_contains"):
        exp = _value(a, case)
        cs = a.get("case_sensitive", False)
        if isinstance(actual, list):
            hit = exp in actual or (not cs and isinstance(exp, str) and exp.lower() in [str(x).lower() for x in actual])
        else:
            hay, needle = _text(actual), _text(exp)
            hit = needle in hay if cs else needle.lower() in hay.lower()
        ok = hit if t == "contains" else not hit
        return ok, f"{'found' if hit else 'not found'}: {_text(exp)[:80]}"
    if t == "regex":
        ok = re.search(a["pattern"], _text(actual)) is not None
        return ok, f"pattern {'matched' if ok else 'not matched'}"
    if t == "min_items":
        n = len(actual) if isinstance(actual, (list, dict, str)) else 0
        return n >= a["min"], f"items={n}"
    raise DatasetError(f"assertion {t} cannot be checked synchronously")


# ------------------------------------------------------------------------ runner

JUDGE_SYSTEM = (
    "You are a strict evaluator. Score how well the OUTPUT satisfies the RUBRIC for the given INPUT "
    "(and EXPECTED, if present). Return JSON {\"score\": number between 0 and 1, \"reason\": string}."
)


def judge_release(model: str, catalog_model: dict[str, Any] | None, policy_soft: list[str]) -> dict[str, Any]:
    definition = {
        "schema_version": "1",
        "name": "eval-judge",
        "owner_bu": "platform",
        "prompt": {"system": JUDGE_SYSTEM},
        "model": {"catalog_id": model},
        "pattern": {"type": "single_shot", "params": {}},
        "output": {
            "format": "json",
            "json_schema": {
                "type": "object",
                "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
                "required": ["score", "reason"],
            },
        },
    }
    return {
        "version": "judge",
        "content_hash": "judge",
        "definition": definition,
        "limits": {"max_cost_usd": 0.5, "max_output_tokens": 1024, "max_latency_ms": 60000, "max_tool_calls": 0,
                   "max_iterations": 1, "max_depth": 0, "max_input_tokens": 100000},
        "guardrails": [],
        "soft_controls": policy_soft,
        "catalog": {"models": {model: catalog_model} if catalog_model else {}, "tools": {}, "knowledge": {}},
    }


async def run_cases(
    runtime,
    release: dict[str, Any],
    policy: dict[str, Any],
    cases: list[dict[str, Any]],
    suite_assertions: list[dict[str, Any]],
    *,
    principal: str,
    judge_model: str,
    models_catalog: dict[str, dict[str, Any]],
    concurrency: int = 4,
) -> dict[str, Any]:
    sem = asyncio.Semaphore(concurrency)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                result = await runtime.execute(
                    {"release": release, "policy": policy, "input": case["input"], "principal": principal,
                     "agent": release["definition"]["name"], "context": case.get("context")}
                )
            except Exception as e:  # runtime unreachable etc.
                return {"case_id": case["id"], "passed": False, "status": "error", "error": str(e),
                        "assertions": [], "cost_usd": 0.0, "latency_ms": 0}
            checks = []
            cost = (result.get("cost") or {}).get("usd", 0.0)
            assertions = [*suite_assertions, *case.get("assertions", [])]
            if not assertions:
                assertions = [{"type": "status", "equals": "ok"}]
            for a in assertions:
                if a["type"] == "llm_judge":
                    model = a.get("model") or judge_model
                    jr = await runtime.execute(
                        {
                            "release": judge_release(model, models_catalog.get(model), policy.get("soft_controls", [])),
                            "policy": policy,
                            "principal": principal,
                            "agent": "eval-judge",
                            "input": {"rubric": a["rubric"], "input": case["input"], "expected": case.get("expected"),
                                      "output": result.get("output")},
                        }
                    )
                    cost += (jr.get("cost") or {}).get("usd", 0.0)
                    out = jr.get("output") if isinstance(jr.get("output"), dict) else {}
                    score = float(out.get("score", 0) or 0)
                    ok = jr.get("status") == "ok" and score >= a.get("threshold", 0.7)
                    checks.append({"type": "llm_judge", "passed": ok,
                                   "detail": f"score={score:.2f} {str(out.get('reason', ''))[:200]}"})
                    continue
                ok, detail = check(a, result, case)
                checks.append({"type": a["type"], "path": a.get("path"), "passed": ok, "detail": detail})
            return {
                "case_id": case["id"],
                "passed": all(c["passed"] for c in checks),
                "status": result.get("status"),
                "output": result.get("output"),
                "error": result.get("error"),
                "assertions": checks,
                "cost_usd": cost,
                "latency_ms": result.get("latency_ms", 0),
                "run_id": result.get("run_id"),
            }

    results = await asyncio.gather(*(one(c) for c in cases))
    passed = sum(1 for r in results if r["passed"])
    return {
        "results": list(results),
        "passed": passed,
        "total": len(results),
        "pass_rate": passed / len(results) if results else 0.0,
        "cost_usd": sum(r["cost_usd"] for r in results),
    }
