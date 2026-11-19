"""Mock platform adapter (spec section 0.4): records what it would provision.

State is kept in memory, or in a JSON file when `state_file` is set, so a local stack can be
inspected and "deleted from the platform" (acceptance test 9).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapter import (
    SPEC_FIELDS,
    DeploymentResult,
    PlatformState,
    ProvisioningPlan,
    diff_plan,
    render_resources,
)


class MockAdapter:
    id = "mock"
    version = "1.0.0"
    CREDENTIAL = "mock-platform-token"

    def __init__(self, secrets: Any, state_file: str | None = None, unsupported: tuple[str, ...] = ()):
        self.secrets = secrets
        self.state_file = Path(state_file) if state_file else None
        self._unsupported = set(unsupported)
        self.fail_keys: set[str] = set()  # simulate platform errors for tests
        self.log: list[dict[str, Any]] = []
        self._state: dict[str, dict[str, dict[str, Any]]] = {}
        if self.state_file and self.state_file.exists():
            self._state = json.loads(self.state_file.read_text())

    def _save(self) -> None:
        if self.state_file:
            self.state_file.write_text(json.dumps(self._state, indent=2, sort_keys=True))

    def supported_fields(self) -> list[str]:
        return [f for f in SPEC_FIELDS if f not in self._unsupported]

    def plan(self, spec: dict[str, Any], current: PlatformState | None = None, environment: str = "dev") -> ProvisioningPlan:
        return diff_plan(spec["agentId"], environment, render_resources(spec), current)

    async def apply(self, plan: ProvisioningPlan) -> DeploymentResult:
        await self.secrets.get(self.CREDENTIAL)  # C10-F6: fails without a credential
        key = f"{plan.environment}:{plan.agent_id}"
        resources = self._state.setdefault(key, {})
        applied = []
        for a in plan.actions:
            if a.key in self.fail_keys:
                return DeploymentResult("failed", applied, f"platform rejected {a.key}")
            if a.op == "delete":
                resources.pop(a.key, None)
            else:
                resources[a.key] = a.desired or {}
            applied.append(f"{a.op} {a.key}")
            self.log.append({"env": plan.environment, "op": a.op, "key": a.key})
        self._save()
        return DeploymentResult("succeeded", applied, None, PlatformState(plan.agent_id, dict(resources)).fingerprint)

    async def read_state(self, agent_id: str, environment: str = "dev") -> PlatformState:
        return PlatformState(agent_id, dict(self._state.get(f"{environment}:{agent_id}", {})))

    # test / ops helper: simulate someone deleting the agent on the platform
    def delete_agent(self, agent_id: str, environment: str = "dev") -> None:
        self._state.pop(f"{environment}:{agent_id}", None)
        self._save()
