"""Gemini Enterprise Agent Platform adapter (ADR-002) - skeleton, not wired yet.

Planned mapping of the platform-neutral resources (eap_compiler.adapter.render_resources):

  agent/<id>        Agent Runtime deployment (reasoningEngines resource) for pro-code agents built
                    with ADK + the C09 SDK; Agent Studio / Agent Designer agent for lowCode agents
                    (only the stable "shell": first step calls get_active_profile on C08)
  identity/<id>     Agent Identity (identity_type=AGENT_IDENTITY at create time) + IAM bindings
                    for permissionScopes
  tool/<id>/<t>     MCP tool registration behind Agent Gateway / Apigee MCP proxy
  knowledge/<id>/<s> RAG Engine corpus or Vertex AI Search data store binding
  guardrails/<id>   Model Armor template referenced by the agent; floor settings stay project-level
  channel/<id>/<c>  Gemini Enterprise app publication, A2A card, or API endpoint

Credentials: the adapter runs as the compiler's service account; no secrets in configuration (C10-F6).
Implement once spec open question 1 is answered; keep the mock adapter as the CI default.
"""

from __future__ import annotations

from typing import Any

from ..adapter import SPEC_FIELDS, DeploymentResult, PlatformState, ProvisioningPlan, diff_plan, render_resources


class AgentPlatformAdapter:
    id = "agent-platform"
    version = "0.0.1"

    def __init__(self, project: str, location: str = "europe-west4"):
        self.project = project
        self.location = location

    def supported_fields(self) -> list[str]:
        return list(SPEC_FIELDS)

    def plan(self, spec: dict[str, Any], current: PlatformState | None = None, environment: str = "dev") -> ProvisioningPlan:
        return diff_plan(spec["agentId"], environment, render_resources(spec), current)

    async def apply(self, plan: ProvisioningPlan) -> DeploymentResult:  # pragma: no cover - pending ADR-002
        raise NotImplementedError("Agent Platform adapter pending spec open question 1 (ADR-002)")

    async def read_state(self, agent_id: str, environment: str = "dev") -> PlatformState:  # pragma: no cover
        raise NotImplementedError("Agent Platform adapter pending spec open question 1 (ADR-002)")
