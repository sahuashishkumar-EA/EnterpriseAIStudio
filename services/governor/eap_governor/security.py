"""Per-BU role-based access control for the Governor.

Roles (granted per BU, or platform-wide with bu='*'):
  viewer          read agents, versions, evals, audit
  author          create agents, edit drafts, manage eval suites, run evals
  approver        approve / reject a draft content hash
  publisher       publish versions, move aliases, rollback
  bu_admin        all of the above for the BU + BU policy overlay + BU role bindings
  platform_admin  everything, everywhere: catalog, platform policy

Roles come from the role_bindings table (IAP console users) and from token claims /
dev headers ("bu:role"). Separation of duties is enforced in the release gate, not here.
"""

from __future__ import annotations

from eap_core.auth import Principal
from eap_core.db import Tx, now_iso

ROLES = ("viewer", "author", "approver", "publisher", "bu_admin", "platform_admin")
IMPLIES: dict[str, set[str]] = {
    "viewer": {"viewer"},
    "author": {"author", "viewer"},
    "approver": {"approver", "viewer"},
    "publisher": {"publisher", "viewer"},
    "bu_admin": {"bu_admin", "author", "approver", "publisher", "viewer"},
    "platform_admin": set(ROLES),
}


class Forbidden(Exception):
    pass


class RoleSet:
    def __init__(self, principal: Principal, grants: dict[str, set[str]]):
        self.principal = principal
        self.grants = grants  # bu -> roles (unexpanded)

    def _expanded(self, bu: str) -> set[str]:
        out: set[str] = set()
        for r in self.grants.get(bu, set()) | self.grants.get("*", set()):
            out |= IMPLIES.get(r, set())
        return out

    def has(self, bu: str, role: str) -> bool:
        return role in self._expanded(bu)

    def require(self, bu: str, role: str) -> None:
        if not self.has(bu, role):
            raise Forbidden(f"{self.principal.id} lacks role {role} for BU {bu}")

    def is_platform_admin(self) -> bool:
        return "platform_admin" in self.grants.get("*", set())

    def visible_bus(self) -> set[str] | None:
        """None = all BUs."""
        if "*" in self.grants:
            return None
        return set(self.grants)

    def to_dict(self) -> dict[str, list[str]]:
        return {bu: sorted(rs) for bu, rs in self.grants.items()}


def load_roles(tx: Tx, principal: Principal) -> RoleSet:
    grants: dict[str, set[str]] = {}
    for bu, roles in principal.bu_roles().items():
        grants.setdefault(bu, set()).update(r for r in roles if r in ROLES)
    for row in tx.fetchall("SELECT bu, role FROM role_bindings WHERE principal = ?", (principal.id,)):
        grants.setdefault(row["bu"], set()).add(row["role"])
    return RoleSet(principal, grants)


def grant(tx: Tx, principal: str, bu: str, role: str, granted_by: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    if role == "platform_admin" and bu != "*":
        raise ValueError("platform_admin must be granted with bu='*'")
    tx.execute(
        "INSERT INTO role_bindings (principal, bu, role, granted_by, granted_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (principal, bu, role) DO NOTHING",
        (principal, bu, role, granted_by, now_iso()),
    )


def revoke(tx: Tx, principal: str, bu: str, role: str) -> int:
    return tx.execute("DELETE FROM role_bindings WHERE principal = ? AND bu = ? AND role = ?", (principal, bu, role))
