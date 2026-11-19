"""C10 platform adapter interface (spec 7.5) and the platform-neutral spec it consumes.

    class PlatformAdapter:
        id: str
        supported_fields() -> list[str]                          C10-F7
        plan(spec, current=None) -> ProvisioningPlan
        apply(plan) -> DeploymentResult                         idempotent, C10-F3
        read_state(agent_id, env) -> PlatformState                    drift detection, C10-F4
        run_evaluation(suite, target) -> dict    (optional)      used by C06
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from eap_metamodel.resolve import resolve_config

SPEC_FIELDS = ("agent", "identity", "contract", "reasoning", "profiles", "tools", "knowledge", "guardrails",
               "model", "channels")


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass
class PlatformState:
    agent_id: str
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return _hash(self.resources)


@dataclass
class Action:
    op: str  # create | update | delete
    key: str
    desired: dict[str, Any] | None = None


@dataclass
class ProvisioningPlan:
    agent_id: str
    environment: str
    actions: list[Action] = field(default_factory=list)
    desired: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.actions


@dataclass
class DeploymentResult:
    status: str  # succeeded | failed
    applied: list[str] = field(default_factory=list)
    error: str | None = None
    state_fingerprint: str | None = None


class UnsupportedFields(Exception):
    def __init__(self, adapter: str, fields: list[str]):
        super().__init__(f"adapter {adapter} does not support: {', '.join(fields)}")
        self.fields = fields


class PlatformAdapter(Protocol):
    id: str
    version: str

    def supported_fields(self) -> list[str]: ...

    def plan(self, spec: dict[str, Any], current: PlatformState | None = None, environment: str = "dev") -> ProvisioningPlan: ...

    async def apply(self, plan: ProvisioningPlan) -> DeploymentResult: ...

    async def read_state(self, agent_id: str, environment: str = "dev") -> PlatformState: ...


# ---------------------------------------------------------------------------- spec compiler


def compile_spec(snapshot: dict[str, list[dict[str, Any]]], agent_id: str) -> dict[str, Any]:
    """Render the active (or candidate) snapshot of one agent into the platform-neutral spec (C10-F1)."""
    agent_e = next((a for a in snapshot.get("agent", []) if a["name"] == agent_id), None)
    if agent_e is None:
        raise KeyError(f"agent {agent_id} not in snapshot")
    agent = agent_e["content"]

    def ref(type_: str, name: str | None) -> dict[str, Any] | None:
        return next((e for e in snapshot.get(type_, []) if e["name"] == name), None)

    profiles = {}
    for p in snapshot.get("profile", []):
        if p["content"].get("agentId") != agent_id:
            continue
        bu = p["content"]["scope"]["businessUnit"]
        r = resolve_config(snapshot, agent_id, bu)
        profiles[bu] = {"versionId": p["versionId"], "persona": r["profile"].get("persona"),
                        "outputFormat": r["profile"].get("outputFormat")}
    any_bu = next(iter(profiles), None)
    resolved = resolve_config(snapshot, agent_id, any_bu) if any_bu else {"bindings": {"tools": [], "knowledge": {}, "model": {}}, "policy": {}}
    identity = ref("identity_ref", agent.get("identityRef"))
    contract = ref("contract", agent.get("contractRef"))
    reasoning = ref("reasoning", agent.get("reasoningRef"))
    guard = ref("guardrail_policy", agent.get("guardrailPolicyRef"))
    # agent-wide tool / knowledge bindings: every BU shares them (cold config)
    return {
        "agentId": agent_id,
        "agentVersionId": agent_e["versionId"],
        "agent": {"name": agent["name"], "boundedOutcome": agent["boundedOutcome"], "riskTier": agent["riskTier"],
                  "solutionType": agent["solutionType"], "lowCode": agent.get("platform", {}).get("lowCode", False)},
        "identity": identity and identity["content"],
        "contract": contract and contract["content"],
        "reasoning": reasoning and reasoning["content"],
        "profiles": profiles,
        "tools": resolved["bindings"]["tools"],
        "knowledge": resolved["bindings"]["knowledge"],
        "guardrails": guard and {"policyRefs": guard["content"]["policyRefs"],
                                 "redactOutputs": guard["content"].get("privacyRules", {}).get("redactOutputs", [])},
        "model": resolved["bindings"]["model"],
        "channels": agent.get("channels", []),
    }


def check_supported(adapter: PlatformAdapter, spec: dict[str, Any]) -> None:
    """Fail early for fields the adapter cannot render (C10-F7)."""
    supported = set(adapter.supported_fields())
    used = [f for f in SPEC_FIELDS if spec.get(f) not in (None, [], {}, "")]
    missing = [f for f in used if f not in supported]
    if missing:
        raise UnsupportedFields(adapter.id, missing)


def render_resources(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Platform-neutral desired resources keyed by a stable id. Adapters map these to vendor objects."""
    a = spec["agentId"]
    res: dict[str, dict[str, Any]] = {
        f"agent/{a}": {"agent": spec["agent"], "contract": spec.get("contract"), "reasoning": spec.get("reasoning"),
                       "model": spec.get("model"), "profiles": sorted(spec.get("profiles", {}))},
    }
    if spec.get("identity"):
        res[f"identity/{a}"] = spec["identity"]
    if spec.get("guardrails"):
        res[f"guardrails/{a}"] = spec["guardrails"]
    for t in spec.get("tools", []):
        res[f"tool/{a}/{t['toolDefId']}"] = {k: t[k] for k in ("mcpServerId", "toolName", "endpoint")}
    for s in spec.get("knowledge", {}).get("sources", []):
        res[f"knowledge/{a}/{s}"] = {"source": s, "filters": spec["knowledge"].get("filters", {})}
    for ch in spec.get("channels", []):
        res[f"channel/{a}/{ch}"] = {"channel": ch}
    return res


def diff_plan(agent_id: str, environment: str, desired: dict[str, dict[str, Any]],
              current: PlatformState | None) -> ProvisioningPlan:
    cur = (current.resources if current else {}) or {}
    actions = []
    for k in sorted(desired):
        if k not in cur:
            actions.append(Action("create", k, desired[k]))
        elif _hash(cur[k]) != _hash(desired[k]):
            actions.append(Action("update", k, desired[k]))
    for k in sorted(set(cur) - set(desired)):
        actions.append(Action("delete", k))
    return ProvisioningPlan(agent_id, environment, actions, desired)


async def drift(adapter: PlatformAdapter, spec: dict[str, Any], environment: str) -> list[dict[str, Any]]:
    """C10-F4: differences between the released spec and the platform's live state."""
    live = await adapter.read_state(spec["agentId"], environment)
    plan = adapter.plan(spec, live, environment)
    return [{"op": a.op, "key": a.key} for a in plan.actions]
