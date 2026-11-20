"""Business units and people.

- business_units: the BUs that exist (name, description, chat settings).
- users: the directory (display name, status, default BU, attributes such as language).
- membership = role bindings per BU (role_bindings, principal = email). Any role in a BU makes the
  person a member of it; "member" alone means an end user of the BU's agents in the chat app.

Platform admins manage everything; a BU admin manages the people of their own BU(s).
"""

from __future__ import annotations

import re
from typing import Any

from eap_core.db import Tx, dumps, loads, now_iso

from . import security
from .store import Conflict, Invalid, NotFound, audit

BU_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLE_LABELS = {
    "member": "Uses the BU's agents (chat)",
    "viewer": "Sees agents, tests and history",
    "functional_author": "Shapes behaviour (functional studio)",
    "author": "Technical setup",
    "approver": "Approves releases",
    "publisher": "Puts versions live",
    "bu_admin": "Manages the BU and its people",
}


# ------------------------------------------------------------------ business units

def _bu_row(r: dict[str, Any]) -> dict[str, Any]:
    r["settings"] = loads(r.get("settings"), {})
    return r


def ensure_bu(tx: Tx, bu: str, actor: str) -> None:
    if bu == "*" or tx.fetchone("SELECT id FROM business_units WHERE id = ?", (bu,)):
        return
    if not BU_RE.match(bu):
        raise Invalid("business unit ids are lowercase letters, digits and dashes, e.g. retail")
    tx.execute("INSERT INTO business_units (id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
               (bu, bu.replace("-", " ").title(), actor, now_iso()))
    audit(tx, actor, "bu.create", f"bu/{bu}", {})


def get_bu(tx: Tx, bu: str) -> dict[str, Any]:
    r = tx.fetchone("SELECT * FROM business_units WHERE id = ?", (bu,))
    if r is None:
        raise NotFound(f"business unit {bu} not found")
    return _bu_row(r)


def list_bus(tx: Tx, visible: set[str] | None = None) -> list[dict[str, Any]]:
    rows = [_bu_row(r) for r in tx.fetchall("SELECT * FROM business_units ORDER BY name")]
    if visible is not None:
        rows = [r for r in rows if r["id"] in visible]
    members: dict[str, set[str]] = {}
    for b in tx.fetchall("SELECT DISTINCT principal, bu FROM role_bindings WHERE bu <> '*'"):
        members.setdefault(b["bu"], set()).add(b["principal"])
    agents: dict[str, int] = {}
    for a in tx.fetchall("SELECT owner_bu, COUNT(*) AS n FROM agents WHERE archived = 0 GROUP BY owner_bu"):
        agents[a["owner_bu"]] = a["n"]
    for r in rows:
        r["members"] = len(members.get(r["id"], set()))
        r["agents"] = agents.get(r["id"], 0)
    return rows


def create_bu(tx: Tx, data: dict[str, Any], actor: str) -> dict[str, Any]:
    bu = str(data.get("id", "")).strip().lower()
    if not BU_RE.match(bu):
        raise Invalid("business unit ids are lowercase letters, digits and dashes, e.g. retail")
    if tx.fetchone("SELECT id FROM business_units WHERE id = ?", (bu,)):
        raise Conflict(f"business unit {bu} already exists")
    tx.execute("INSERT INTO business_units (id, name, description, settings, created_by, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (bu, data.get("name") or bu.title(), data.get("description", ""), dumps(data.get("settings") or {}),
                actor, now_iso()))
    audit(tx, actor, "bu.create", f"bu/{bu}", {"name": data.get("name")})
    return get_bu(tx, bu)


def update_bu(tx: Tx, bu: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
    cur = get_bu(tx, bu)
    name = data.get("name", cur["name"])
    desc = data.get("description", cur["description"])
    status = data.get("status", cur["status"])
    if status not in ("active", "archived"):
        raise Invalid("status must be active or archived")
    settings = {**cur["settings"], **(data.get("settings") or {})}
    tx.execute("UPDATE business_units SET name = ?, description = ?, status = ?, settings = ? WHERE id = ?",
               (name, desc, status, dumps(settings), bu))
    audit(tx, actor, "bu.update", f"bu/{bu}", {k: data[k] for k in data if k != "settings"})
    return get_bu(tx, bu)


# ------------------------------------------------------------------ people

def _user(tx: Tx, email: str) -> dict[str, Any]:
    u = tx.fetchone("SELECT * FROM users WHERE email = ?", (email,))
    u = dict(u) if u else {"email": email, "display_name": "", "status": "active", "default_bu": None,
                           "attributes": "{}", "created_by": None, "created_at": None, "last_seen_at": None,
                           "in_directory": False}
    u.setdefault("in_directory", True)
    u["attributes"] = loads(u.get("attributes"), {})
    roles: dict[str, list[str]] = {}
    for b in tx.fetchall("SELECT bu, role FROM role_bindings WHERE principal = ? ORDER BY bu, role", (email,)):
        roles.setdefault(b["bu"], []).append(b["role"])
    u["memberships"] = roles
    return u


def get_user(tx: Tx, email: str) -> dict[str, Any]:
    return _user(tx, email)


def list_users(tx: Tx, visible: set[str] | None = None, bu: str | None = None) -> list[dict[str, Any]]:
    emails = {r["email"] for r in tx.fetchall("SELECT email FROM users")}
    emails |= {r["principal"] for r in tx.fetchall("SELECT DISTINCT principal FROM role_bindings")}
    out = []
    for e in sorted(emails):
        u = _user(tx, e)
        if bu and bu not in u["memberships"]:
            continue
        if visible is not None:
            # BU admins see the people of their BUs (only those memberships)
            u["memberships"] = {k: v for k, v in u["memberships"].items() if k in visible}
            if not u["memberships"]:
                continue
        out.append(u)
    return out


def upsert_user(tx: Tx, email: str, data: dict[str, Any], actor: str, *, may_change_status: bool) -> dict[str, Any]:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise Invalid("enter a valid email address")
    cur = tx.fetchone("SELECT * FROM users WHERE email = ?", (email,))
    status = data.get("status")
    if status is not None and status not in ("active", "disabled"):
        raise Invalid("status must be active or disabled")
    if status is not None and not may_change_status and status != (cur["status"] if cur else "active"):
        raise security.Forbidden("only platform admins can disable or re-enable people")
    attrs = data.get("attributes")
    if cur is None:
        tx.execute("INSERT INTO users (email, display_name, status, default_bu, attributes, created_by, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (email, data.get("display_name", ""), status or "active", data.get("default_bu"),
                    dumps(attrs or {}), actor, now_iso()))
        audit(tx, actor, "user.create", f"user/{email}", {"display_name": data.get("display_name", "")})
    else:
        tx.execute("UPDATE users SET display_name = ?, status = ?, default_bu = ?, attributes = ? WHERE email = ?",
                   (data.get("display_name", cur["display_name"]), status or cur["status"],
                    data.get("default_bu", cur["default_bu"]),
                    dumps(attrs if attrs is not None else loads(cur["attributes"], {})), email))
        audit(tx, actor, "user.update", f"user/{email}", {k: v for k, v in data.items() if k != "attributes"})
    return _user(tx, email)


def set_membership(tx: Tx, email: str, bu: str, roles: list[str], actor: str) -> dict[str, Any]:
    """Replace the person's roles in one BU (empty list = remove from the BU)."""
    email = email.strip().lower()
    bad = [r for r in roles if r not in security.ROLES or r == "platform_admin"]
    if bad:
        raise Invalid(f"unknown role(s): {', '.join(bad)}")
    ensure_bu(tx, bu, actor)
    if not tx.fetchone("SELECT email FROM users WHERE email = ?", (email,)):
        upsert_user(tx, email, {}, actor, may_change_status=False)
    before = {r["role"] for r in tx.fetchall("SELECT role FROM role_bindings WHERE principal = ? AND bu = ?", (email, bu))}
    for r in before - set(roles):
        security.revoke(tx, email, bu, r)
    for r in set(roles) - before:
        security.grant(tx, email, bu, r, actor)
    audit(tx, actor, "membership.set", f"bu/{bu}", {"user": email, "roles": sorted(roles), "before": sorted(before)})
    return _user(tx, email)


def chat_profile(tx: Tx, email: str) -> dict[str, Any]:
    """What the chat app needs after sign-in: who the person is and which BUs they may act for."""
    u = _user(tx, email)
    if u["status"] == "disabled":
        return {**u, "bus": []}
    rows = {r["id"]: r for r in list_bus(tx)}
    bus = [{"id": bu, "name": rows[bu]["name"], "roles": roles, "settings": rows[bu]["settings"]}
           for bu, roles in u["memberships"].items() if bu in rows and rows[bu]["status"] == "active"]
    if "*" in u["memberships"]:  # platform-wide grants: every active BU
        have = {b["id"] for b in bus}
        bus += [{"id": r["id"], "name": r["name"], "roles": u["memberships"]["*"], "settings": r["settings"]}
                for r in rows.values() if r["status"] == "active" and r["id"] not in have]
    tx.execute("UPDATE users SET last_seen_at = ? WHERE email = ?", (now_iso(), email))
    return {"email": email, "display_name": u["display_name"], "status": u["status"], "default_bu": u["default_bu"],
            "attributes": u["attributes"], "bus": sorted(bus, key=lambda b: b["name"])}
