"""Resolution: agent spec + behavior profile + bindings -> one runtime configuration (C07-F1/F2).

Pure functions over a *snapshot*: the active version of every artifact in one environment,
optionally with candidate versions overlaid (the evaluation runner resolves drafts this way).

    snapshot = {"agent": [{"artifactId", "name", "versionId", "version", "content"}, ...], ...}

Binding precedence: a binding scoped to the business unit overrides an agent-wide binding of
the same bindingType. Values outside the guardrail policy's limits are clamped and reported
(the Registry API rejects them on write; this is the second, runtime line of defence).
"""

from __future__ import annotations

import copy
from typing import Any

from .metamodel import Metamodel, get_pointer


class ResolutionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


HOT_PROFILE_FIELDS = ("persona", "instructions", "examples", "outputFormat", "escalationContacts", "scope")


def _by_name(snapshot: dict[str, list[dict[str, Any]]], type_: str, name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    for e in snapshot.get(type_, []):
        if e["name"] == name:
            return e
    return None


def _clamp(content: dict[str, Any], key: str, limits: list[dict[str, Any]], warnings: list[str]) -> None:
    for lim in limits:
        if lim.get("appliesTo") != key:
            continue
        parts = [p for p in lim["path"].lstrip("/").split("/") if p]
        parent = get_pointer(content, "/" + "/".join(parts[:-1])) if len(parts) > 1 else content
        if not isinstance(parent, dict) or parts[-1] not in parent:
            continue
        v = parent[parts[-1]]
        if isinstance(v, (int, float)):
            if "maximum" in lim and v > lim["maximum"]:
                parent[parts[-1]] = lim["maximum"]
                warnings.append(f"{key}{lim['path']} clamped to {lim['maximum']}")
            if "minimum" in lim and v < lim["minimum"]:
                parent[parts[-1]] = lim["minimum"]
                warnings.append(f"{key}{lim['path']} clamped to {lim['minimum']}")
        if "enum" in lim and v not in lim["enum"]:
            parent.pop(parts[-1])
            warnings.append(f"{key}{lim['path']} {v!r} not allowed; dropped")


def resolve_config(snapshot: dict[str, list[dict[str, Any]]], agent_id: str, business_unit: str,
                   mm: Metamodel | None = None) -> dict[str, Any]:
    agent_e = _by_name(snapshot, "agent", agent_id)
    if agent_e is None:
        raise ResolutionError("agent_not_active", f"agent {agent_id!r} has no active version")
    agent = agent_e["content"]
    profile_e = next((p for p in snapshot.get("profile", [])
                      if p["content"].get("agentId") == agent_id
                      and p["content"].get("scope", {}).get("businessUnit") == business_unit), None)
    if profile_e is None:
        raise ResolutionError("profile_not_active", f"no active profile for {agent_id!r} / {business_unit!r}")

    versions: dict[str, str] = {"agent": agent_e["versionId"], "profile": profile_e["versionId"]}
    warnings: list[str] = []

    guard_e = _by_name(snapshot, "guardrail_policy", agent.get("guardrailPolicyRef"))
    limits = (guard_e or {}).get("content", {}).get("limits", [])
    if guard_e:
        versions["guardrailPolicy"] = guard_e["versionId"]
    model_policy_e = _by_name(snapshot, "model_policy", agent.get("modelPolicyRef"))
    model_policy = (model_policy_e or {}).get("content", {})
    if model_policy_e:
        versions["modelPolicy"] = model_policy_e["versionId"]

    profile = copy.deepcopy(profile_e["content"])
    _clamp(profile, "profile", limits, warnings)

    # bindings: agent-wide first, then BU-specific overrides of the same type
    chosen: dict[str, dict[str, Any]] = {}
    for b in sorted(snapshot.get("binding", []), key=lambda e: 1 if e["content"].get("businessUnit") else 0):
        c = b["content"]
        if c.get("agentId") != agent_id:
            continue
        if c.get("businessUnit") not in (None, business_unit):
            continue
        chosen[c["bindingType"]] = b
    for btype, b in chosen.items():
        versions[f"binding.{btype}"] = b["versionId"]

    def bcontent(t: str) -> dict[str, Any]:
        b = chosen.get(t)
        if not b:
            return {}
        c = copy.deepcopy(b["content"])
        _clamp(c, f"binding:{t}", limits, warnings)
        return c

    skills = []
    for s in bcontent("skill").get("skills", []):
        se = _by_name(snapshot, "skill", s["skillId"])
        if se is None:
            warnings.append(f"skill {s['skillId']} not active; skipped")
            continue
        versions[f"skill.{se['name']}"] = se["versionId"]
        sc = se["content"]
        skills.append({"name": sc["name"], "description": sc["description"], "instructions": sc["instructions"],
                       "toolsUsed": sc.get("toolsUsed", [])})

    tools = []
    for t in bcontent("tool").get("tools", []):
        te = _by_name(snapshot, "tool_def", t["toolDefId"])
        if te is None:
            warnings.append(f"tool {t['toolDefId']} not active; skipped")
            continue
        versions[f"tool.{te['name']}"] = te["versionId"]
        tc = te["content"]
        tools.append({"capability": t["capability"], "toolDefId": te["name"], "mcpServerId": tc["mcpServerId"],
                      "toolName": tc["toolName"], "endpoint": tc.get("endpoint"), "thresholdKeys": tc.get("thresholdKeys", [])})

    kb = bcontent("knowledge")
    knowledge = {"sources": [s["knowledgeSourceId"] for s in kb.get("sources", [])], "filters": kb.get("filters", {})}

    approved = model_policy.get("approvedTiers", [])
    tier = bcontent("model").get("tier") or model_policy.get("defaultTier")
    if approved and tier not in approved:
        warnings.append(f"model tier {tier!r} not in model policy; using default")
        tier = model_policy.get("defaultTier")

    thr = bcontent("thresholds")
    test_set = bcontent("test_set")

    return {
        "agent": {k: agent.get(k) for k in ("id", "name", "boundedOutcome", "riskTier", "solutionType", "channels")},
        "profile": {k: profile.get(k) for k in HOT_PROFILE_FIELDS if k in profile},
        "bindings": {
            "skills": skills,
            "tools": tools,
            "knowledge": knowledge,
            "model": {"tier": tier, "fallbackTier": model_policy.get("fallbackTier"),
                      "usageLimits": model_policy.get("usageLimits", {})},
        },
        "thresholds": {"values": thr.get("values", {}), "approvers": thr.get("approvers", []),
                       "escalationQueue": thr.get("escalationQueue")},
        "policy": {"redactOutputs": (guard_e or {}).get("content", {}).get("privacyRules", {}).get("redactOutputs", [])},
        "testSet": {"cases": test_set.get("cases", []), "targets": test_set.get("targets", {})} if test_set else None,
        "evalSuiteRef": agent.get("evalSuiteRef"),
        "versions": versions,
        "warnings": warnings,
    }


def hot_view(resolved: dict[str, Any]) -> dict[str, Any]:
    """The subset a low-code agent receives from the config MCP server (C08-F2)."""
    p = resolved.get("profile", {})
    return {
        "persona": p.get("persona"),
        "instructions": p.get("instructions"),
        "output_format": p.get("outputFormat"),
        "thresholds": resolved.get("thresholds", {}).get("values", {}),
        "escalation_contacts": p.get("escalationContacts", []),
        "versions": resolved.get("versions", {}),
    }
