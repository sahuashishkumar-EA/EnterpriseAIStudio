"""C06 Evaluation runner.

Runs the agent's evaluation suite and the profile's BU test set against a *candidate*
configuration (the active snapshot with the change request's versions overlaid), compares the
scores with the currently active configuration, and stores one evaluation_run per version.

Evaluators:
  MockEvaluator      deterministic fake LLM for local runs and CI (spec section 2.3)
  AdapterEvaluator   the target platform's evaluation service through a C10 adapter (C06-F2),
                     e.g. the Gen AI evaluation service on Gemini Enterprise Agent Platform
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from eap_core.db import Database, Tx, dumps, new_id, now_iso
from eap_metamodel import Metamodel
from eap_metamodel.resolve import ResolutionError, resolve_config
from eap_store import store

log = logging.getLogger("eap.evalrunner")


class Evaluator(Protocol):
    name: str

    async def respond(self, resolved: dict[str, Any], case_input: str) -> str: ...


class MockEvaluator:
    """Scripted fake LLM: answers with the persona, the instructions and the input, cut to the
    profile's output length. Good enough to test persona, instruction and length rules."""

    name = "mock"

    async def respond(self, resolved: dict[str, Any], case_input: str) -> str:
        p = resolved.get("profile", {})
        persona = p.get("persona") or {}
        text = f"{persona.get('name', 'Assistant')} ({persona.get('tone', 'neutral')}): {p.get('instructions', '')} | {case_input}"
        limit = (p.get("outputFormat") or {}).get("maxLength")
        return text[:limit] if limit else text


class AdapterEvaluator:
    """Delegates to PlatformAdapter.run_evaluation (C06-F2)."""

    def __init__(self, adapter: Any, fallback: Evaluator | None = None):
        self.adapter = adapter
        self.fallback = fallback or MockEvaluator()
        self.name = f"adapter:{adapter.id}"

    async def respond(self, resolved: dict[str, Any], case_input: str) -> str:
        fn = getattr(self.adapter, "respond", None)
        if fn is None:
            return await self.fallback.respond(resolved, case_input)
        return await fn(resolved, case_input)


def check_case(case: dict[str, Any], output: str) -> list[str]:
    exp = case.get("expect") or {}
    low = output.lower()
    fails = [f"missing {s!r}" for s in exp.get("contains", []) if s.lower() not in low]
    fails += [f"contains forbidden {s!r}" for s in exp.get("notContains", []) if s.lower() in low]
    if exp.get("maxLength") and len(output) > exp["maxLength"]:
        fails.append(f"longer than {exp['maxLength']}")
    return fails


@dataclass
class SuiteResult:
    pass_rate: float
    cases: list[dict[str, Any]] = field(default_factory=list)


async def run_cases(evaluator: Evaluator, resolved: dict[str, Any], cases: list[dict[str, Any]]) -> SuiteResult:
    out = []
    for c in cases:
        try:
            text = await evaluator.respond(resolved, c["input"])
            fails = check_case(c, text)
        except Exception as e:  # an erroring case is a failed case
            text, fails = "", [f"error: {e}"]
        out.append({"id": c["id"], "passed": not fails, "failures": fails})
    rate = sum(1 for c in out if c["passed"]) / len(out) if out else 1.0
    return SuiteResult(rate, out)


@dataclass
class Target:
    agent_id: str
    business_unit: str


def affected_targets(tx: Tx, versions: list[dict[str, Any]], environment: str,
                     snap: dict[str, list[dict[str, Any]]]) -> list[Target]:
    """Agent x BU pairs a change can affect."""
    agents: set[str] = set()
    bus: dict[str, set[str]] = {}
    for v in versions:
        art = store.get_artifact(tx, v["artifact_id"])
        c = v["content"]
        if art["type"] == "agent":
            agents.add(c["id"])
        elif art["type"] in ("profile", "binding"):
            agents.add(c["agentId"])
            bu = c.get("businessUnit") or (c.get("scope") or {}).get("businessUnit")
            if bu:
                bus.setdefault(c["agentId"], set()).add(bu)
        else:  # shared artifact (skill, policy, suite, tool): every agent that is active here
            agents.update(e["name"] for e in snap.get("agent", []))
    targets = []
    for a in sorted(agents):
        all_bus = sorted({p["content"]["scope"]["businessUnit"] for p in snap.get("profile", [])
                          if p["content"].get("agentId") == a})
        for bu in sorted(bus.get(a) or all_bus):
            targets.append(Target(a, bu))
    return targets


class EvalRunner:
    def __init__(self, db: Database, mm: Metamodel, evaluator: Evaluator | None = None):
        self.db = db
        self.mm = mm
        self.evaluator = evaluator or MockEvaluator()

    async def _score(self, snap: dict[str, Any], t: Target) -> dict[str, Any] | None:
        try:
            resolved = resolve_config(snap, t.agent_id, t.business_unit, self.mm)
        except ResolutionError as e:
            return {"error": e.code}
        suite_e = next((s for s in snap.get("eval_suite", []) if s["name"] == resolved.get("evalSuiteRef")), None)
        suite = suite_e["content"] if suite_e else None
        res: dict[str, Any] = {"suiteVersionId": suite_e and suite_e["versionId"], "minPassRate": 1.0}
        if suite:
            r = await run_cases(self.evaluator, resolved, suite["cases"])
            res["suite"] = {"passRate": r.pass_rate, "cases": r.cases}
            res["minPassRate"] = suite["passCriteria"]["minPassRate"]
            res["maxRegression"] = suite["passCriteria"].get("maxRegression")
        ts = resolved.get("testSet")
        if ts:
            r = await run_cases(self.evaluator, resolved, ts["cases"])
            res["testSet"] = {"passRate": r.pass_rate, "cases": r.cases,
                              "minPassRate": ts["targets"].get("minPassRate", 1.0),
                              "versionId": resolved["versions"].get("binding.test_set")}
        return res

    async def evaluate(self, version_ids: list[str], environment: str, change_request_id: str | None) -> dict[str, Any]:
        """Evaluate candidate versions; returns {"passed", "targets": [...]} and stores evaluation_run rows."""

        def prep(tx: Tx):
            versions = [store.get_version(tx, v) for v in version_ids]
            overlay = {v["artifact_id"]: v["id"] for v in versions}
            current = store.snapshot(tx, environment)
            candidate = store.snapshot(tx, environment, overlay)
            return versions, current, candidate, affected_targets(tx, versions, environment, candidate)

        versions, current, candidate, targets = await self.db.call(prep)
        results, passed = [], True
        if not targets:
            results.append({"note": "no agent x profile affected; nothing to evaluate"})
        for t in targets:
            cand = await self._score(candidate, t)
            base = await self._score(current, t)
            ok, reasons = True, []
            if cand is None or "error" in cand:
                ok, reasons = False, [f"cannot resolve candidate: {cand and cand.get('error')}"]
            else:
                if "suite" in cand and cand["suite"]["passRate"] < cand["minPassRate"]:
                    ok = False
                    reasons.append(f"suite pass rate {cand['suite']['passRate']:.2f} < {cand['minPassRate']:.2f}")
                if cand.get("testSet") and cand["testSet"]["passRate"] < cand["testSet"]["minPassRate"]:
                    ok = False
                    reasons.append(f"BU test set pass rate {cand['testSet']['passRate']:.2f} < {cand['testSet']['minPassRate']:.2f}")
                # C06-F3: regression against the active version
                if base and "suite" in base and "suite" in cand and cand.get("maxRegression") is not None:
                    drop = base["suite"]["passRate"] - cand["suite"]["passRate"]
                    if drop > cand["maxRegression"]:
                        ok = False
                        reasons.append(f"regression {drop:.2f} vs active > {cand['maxRegression']:.2f}")
            passed = passed and ok
            results.append({"agentId": t.agent_id, "businessUnit": t.business_unit, "passed": ok,
                            "reasons": reasons, "candidate": cand, "active": base})

        def save(tx: Tx):
            for v in versions:
                prev = store.active_version(tx, v["artifact_id"], environment)
                suite_vid = next((r["candidate"].get("suiteVersionId") for r in results
                                  if isinstance(r.get("candidate"), dict)), None)
                tx.execute(
                    "INSERT INTO evaluation_run (id, version_id, suite_version_id, test_set_ref, environment, scores, "
                    "passed, compared_to_version_id, change_request_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_id("ev_"), v["id"], suite_vid, None, environment,
                     dumps({"evaluator": self.evaluator.name, "targets": results}), 1 if passed else 0,
                     prev and prev["id"], change_request_id, now_iso()))

        await self.db.call(save)
        return {"passed": passed, "evaluator": self.evaluator.name, "targets": results}

    async def reevaluate_dependents(self, changed_artifact_id: str, environment: str) -> list[dict[str, Any]]:
        """C06-F5: re-run suites for active agents when a model policy, model binding or skill changes."""

        def prep(tx: Tx):
            art = store.get_artifact(tx, changed_artifact_id)
            v = store.active_version(tx, changed_artifact_id, environment)
            return art, v

        art, v = await self.db.call(prep)
        if v is None or art["type"] not in ("skill", "model_policy", "binding"):
            return []
        out = await self.evaluate([v["id"]], environment, None)
        if not out["passed"]:
            log.warning("re-evaluation after %s change failed in %s", art["name"], environment)
        return out["targets"]
