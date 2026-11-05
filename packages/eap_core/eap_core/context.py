"""Signed call context for agent-to-agent calls.

Every run carries a CallContext. When an agent calls another agent the runtime derives a
child context with `child()`, which enforces:
  * identity inheritance  - the child acts on behalf of the same end user (`on_behalf_of`)
  * budget                - the child's budget is carved out of the parent's remaining budget
  * depth                 - depth + 1 must not exceed max_depth (platform cap 3)
  * cycles                - an agent may not appear twice in the call chain
  * trace                 - trace_id is propagated unchanged

The context crosses process boundaries as an HS256 JWT (`X-EAP-Call-Context` header) signed
with a key held in Secret Manager. Only the runtime holds the key.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import jwt

HEADER = "X-EAP-Call-Context"
PLATFORM_MAX_DEPTH = 3
ISSUER = "eap-runtime"


class CallContextError(Exception):
    code = "call_context_error"


class DepthExceeded(CallContextError):
    code = "depth_exceeded"


class CycleDetected(CallContextError):
    code = "cycle_detected"


class BudgetExceeded(CallContextError):
    code = "budget_exceeded"


class InvalidContext(CallContextError):
    code = "invalid_context"


@dataclass(frozen=True)
class CallContext:
    trace_id: str
    run_id: str
    on_behalf_of: str  # original end-user subject
    caller: str  # immediate caller (user subject or "agent:<name>")
    bu: str | None = None
    roles: tuple[str, ...] = ()
    chain: tuple[str, ...] = ()  # agent names, root first
    depth: int = 0
    max_depth: int = PLATFORM_MAX_DEPTH
    budget_usd: float = 1.0  # total budget granted to this context
    deadline: float | None = None  # epoch seconds
    parent_run_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def root(
        *,
        on_behalf_of: str,
        bu: str | None = None,
        roles: tuple[str, ...] | list[str] = (),
        budget_usd: float = 1.0,
        max_depth: int = PLATFORM_MAX_DEPTH,
        deadline: float | None = None,
        trace_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> "CallContext":
        return CallContext(
            trace_id=trace_id or uuid.uuid4().hex,
            run_id=uuid.uuid4().hex,
            on_behalf_of=on_behalf_of,
            caller=on_behalf_of,
            bu=bu,
            roles=tuple(roles),
            budget_usd=float(budget_usd),
            max_depth=min(int(max_depth), PLATFORM_MAX_DEPTH),
            deadline=deadline,
            attributes=dict(attributes or {}),
        )

    def enter(self, agent: str) -> "CallContext":
        """Mark that this context is now executing `agent` (adds it to the chain)."""
        if agent in self.chain:
            raise CycleDetected(f"cycle: {' -> '.join([*self.chain, agent])}")
        return replace(self, chain=(*self.chain, agent))

    def child(self, agent: str, budget_usd: float, *, max_depth: int | None = None) -> "CallContext":
        """Context for calling `agent` from the agent currently at the end of the chain."""
        if agent in self.chain:
            raise CycleDetected(f"cycle: {' -> '.join([*self.chain, agent])}")
        depth = self.depth + 1
        cap = self.max_depth if max_depth is None else min(self.max_depth, max_depth)
        if depth > cap:
            raise DepthExceeded(f"call depth {depth} exceeds max {cap}")
        if budget_usd <= 0:
            raise BudgetExceeded("no budget left for child call")
        if budget_usd > self.budget_usd + 1e-12:
            raise BudgetExceeded(f"child budget {budget_usd:.4f} exceeds parent remaining {self.budget_usd:.4f}")
        if self.deadline is not None and time.time() >= self.deadline:
            raise BudgetExceeded("deadline exceeded")
        return replace(
            self,
            run_id=uuid.uuid4().hex,
            parent_run_id=self.run_id,
            caller=f"agent:{self.chain[-1]}" if self.chain else self.caller,
            chain=(*self.chain, agent),
            depth=depth,
            max_depth=cap,
            budget_usd=float(budget_usd),
        )

    # ------------------------------------------------------------------ wire format

    def to_claims(self) -> dict[str, Any]:
        d = asdict(self)
        d["chain"] = list(self.chain)
        d["roles"] = list(self.roles)
        return d

    @staticmethod
    def from_claims(c: dict[str, Any]) -> "CallContext":
        try:
            return CallContext(
                trace_id=c["trace_id"],
                run_id=c["run_id"],
                on_behalf_of=c["on_behalf_of"],
                caller=c["caller"],
                bu=c.get("bu"),
                roles=tuple(c.get("roles", [])),
                chain=tuple(c.get("chain", [])),
                depth=int(c.get("depth", 0)),
                max_depth=min(int(c.get("max_depth", PLATFORM_MAX_DEPTH)), PLATFORM_MAX_DEPTH),
                budget_usd=float(c.get("budget_usd", 0)),
                deadline=c.get("deadline"),
                parent_run_id=c.get("parent_run_id"),
                attributes=dict(c.get("attributes", {})),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidContext(f"malformed call context: {e}") from e

    def sign(self, key: str | bytes, ttl_s: int = 900) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "iat": now, "exp": now + ttl_s, "ctx": self.to_claims()}
        return jwt.encode(claims, key, algorithm="HS256")

    @staticmethod
    def verify(token: str, key: str | bytes) -> "CallContext":
        try:
            claims = jwt.decode(token, key, algorithms=["HS256"], issuer=ISSUER)
        except jwt.PyJWTError as e:
            raise InvalidContext(f"invalid call context token: {e}") from e
        return CallContext.from_claims(claims["ctx"])
