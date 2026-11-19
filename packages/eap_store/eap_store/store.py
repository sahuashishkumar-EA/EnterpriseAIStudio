"""C03/C04 data access over the append-only registry tables.

Every function takes a transaction (`eap_core.db.Tx`). Nothing here issues UPDATE or DELETE on
an append-only table: drafts are new rows, status and state are new event rows, activation is
a new pointer row (principle 3, ADR-004).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from eap_core.db import Tx, dumps, loads, new_id, now_iso
from eap_metamodel import Metamodel, diff_paths

ENVIRONMENTS = ("dev", "test", "production")


class RegistryError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class NotFound(RegistryError):
    status, code = 404, "not_found"


class Conflict(RegistryError):
    status, code = 409, "conflict"


class Invalid(RegistryError):
    status, code = 422, "invalid"


class Forbidden(RegistryError):
    status, code = 403, "forbidden"


class IntegrityError(RegistryError):
    status, code = 500, "integrity"


def canonical(content: Any) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(content: Any) -> str:
    return hashlib.sha256(canonical(content).encode()).hexdigest()


# ------------------------------------------------------------------------------ audit


def audit(tx: Tx, actor: str, action: str, target: str, version_id: str | None = None,
          detail: dict[str, Any] | None = None) -> None:
    tx.execute(
        "INSERT INTO audit_log (id, actor, action, target, version_id, at, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id("au_"), actor, action, target, version_id, now_iso(), dumps(detail or {})),
    )


def list_audit(tx: Tx, target: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if target:
        rows = tx.fetchall("SELECT * FROM audit_log WHERE target = ? ORDER BY seq DESC LIMIT ?", (target, limit))
    else:
        rows = tx.fetchall("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,))
    return [{**r, "detail": loads(r["detail"], {})} for r in rows]


# ------------------------------------------------------------------------------ artifacts


def _artifact(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("id", "type", "name", "owner", "created_at")}


def get_artifact(tx: Tx, artifact_id: str) -> dict[str, Any]:
    r = tx.fetchone("SELECT * FROM artifact WHERE id = ?", (artifact_id,))
    if not r:
        raise NotFound(f"artifact {artifact_id} not found")
    return _artifact(r)


def find_artifact(tx: Tx, type_: str, name: str) -> dict[str, Any] | None:
    r = tx.fetchone("SELECT * FROM artifact WHERE type = ? AND name = ?", (type_, name))
    return _artifact(r) if r else None


def list_artifacts(tx: Tx, type_: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM artifact WHERE 1=1", []
    if type_:
        sql += " AND type = ?"
        params.append(type_)
    if q:
        sql += " AND (lower(name) LIKE ? OR lower(owner) LIKE ?)"
        params += [f"%{q.lower()}%", f"%{q.lower()}%"]
    return [_artifact(r) for r in tx.fetchall(sql + " ORDER BY type, name", params)]


# ------------------------------------------------------------------------------ versions


def _version(tx: Tx, r: dict[str, Any], verify: bool = True) -> dict[str, Any]:
    content = loads(r["content"])
    if verify and content_hash(content) != r["content_hash"]:  # C03-F4
        raise IntegrityError(f"content hash mismatch for version {r['id']}")
    return {
        "id": r["id"], "artifact_id": r["artifact_id"], "version": r["version"],
        "schema_version": r["schema_version"], "content": content, "content_hash": r["content_hash"],
        "parent_version_id": r["parent_version_id"], "author": r["author"], "created_at": r["created_at"],
        "status": version_status(tx, r["id"], r["status"]),
    }


def version_status(tx: Tx, version_id: str, initial: str = "draft") -> str:
    r = tx.fetchone("SELECT status FROM version_status_event WHERE version_id = ? ORDER BY seq DESC LIMIT 1",
                    (version_id,))
    return r["status"] if r else initial


def set_status(tx: Tx, version_id: str, status: str, actor: str, change_request_id: str | None = None) -> None:
    tx.execute(
        "INSERT INTO version_status_event (id, version_id, status, actor, change_request_id, at) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("vs_"), version_id, status, actor, change_request_id, now_iso()),
    )


def get_version(tx: Tx, version_id: str, verify: bool = True) -> dict[str, Any]:
    r = tx.fetchone("SELECT * FROM artifact_version WHERE id = ?", (version_id,))
    if not r:
        raise NotFound(f"version {version_id} not found")
    return _version(tx, r, verify)


def list_versions(tx: Tx, artifact_id: str) -> list[dict[str, Any]]:
    rows = tx.fetchall("SELECT * FROM artifact_version WHERE artifact_id = ? ORDER BY version DESC", (artifact_id,))
    return [_version(tx, r) for r in rows]


def latest_version(tx: Tx, artifact_id: str) -> dict[str, Any] | None:
    r = tx.fetchone("SELECT * FROM artifact_version WHERE artifact_id = ? ORDER BY version DESC LIMIT 1", (artifact_id,))
    return _version(tx, r) if r else None


def _insert_version(tx: Tx, artifact_id: str, number: int, schema_version: str, content: Any,
                    parent: str | None, author: str) -> str:
    vid = new_id("v_")
    tx.execute(
        "INSERT INTO artifact_version (id, artifact_id, version, schema_version, content, content_hash, "
        "parent_version_id, author, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')",
        (vid, artifact_id, number, schema_version, canonical(content), content_hash(content), parent, author, now_iso()),
    )
    return vid


def validate_content(tx: Tx, mm: Metamodel, type_: str, content: Any) -> list[dict[str, Any]]:
    """Schema + guardrail limits (C03-F2, C01-F4)."""
    errs = mm.validate(type_, content)
    if errs:
        return errs
    if type_ in ("profile", "binding"):
        policy = guardrail_for_agent(tx, content.get("agentId"))
        if policy is not None:
            errs += mm.limit_violations(type_, content, policy.get("limits", []))
    if type_ == "agent":
        for ptr, ref_type, ref in mm.references(type_, content):
            if find_artifact(tx, ref_type, ref) is None:
                errs.append({"path": ptr, "code": "reference", "message": f"{ref_type} {ref!r} does not exist"})
    return errs


def guardrail_for_agent(tx: Tx, agent_id: str | None) -> dict[str, Any] | None:
    """Guardrail policy content that applies to an agent: the one active in production, else the latest."""
    if not agent_id:
        return None
    a = find_artifact(tx, "agent", agent_id)
    if not a:
        return None
    av = active_version(tx, a["id"], "production") or latest_version(tx, a["id"])
    ref = av["content"].get("guardrailPolicyRef") if av else None
    g = find_artifact(tx, "guardrail_policy", ref) if ref else None
    if not g:
        return None
    gv = active_version(tx, g["id"], "production") or latest_version(tx, g["id"])
    return gv["content"] if gv else None


def create_artifact(tx: Tx, mm: Metamodel, type_: str, name: str, owner: str, content: Any, author: str) -> dict[str, Any]:
    if find_artifact(tx, type_, name):
        raise Conflict(f"{type_} {name!r} already exists")
    errs = validate_content(tx, mm, type_, content)
    if errs:
        raise Invalid("content does not validate", errs)
    aid = new_id("a_")
    tx.execute("INSERT INTO artifact (id, type, name, owner, created_at) VALUES (?, ?, ?, ?, ?)",
               (aid, type_, name, owner, now_iso()))
    vid = _insert_version(tx, aid, 1, mm.schema_version(type_), content, None, author)
    audit(tx, author, "artifact.create", aid, vid, {"type": type_, "name": name})
    return {"artifact": get_artifact(tx, aid), "version": get_version(tx, vid)}


def create_draft(tx: Tx, mm: Metamodel, artifact_id: str, base_version_id: str | None, content: Any,
                 author: str) -> dict[str, Any]:
    if not base_version_id:  # C03-F3
        raise Invalid("baseVersionId is required (optimistic locking)")
    art = get_artifact(tx, artifact_id)
    latest = latest_version(tx, artifact_id)
    if latest is None or latest["id"] != base_version_id:
        raise Conflict("draft is based on a stale version", {"baseVersionId": base_version_id,
                                                              "latestVersionId": latest and latest["id"]})
    errs = validate_content(tx, mm, art["type"], content)
    if errs:
        raise Invalid("content does not validate", errs)
    vid = _insert_version(tx, artifact_id, latest["version"] + 1, mm.schema_version(art["type"]), content,
                          base_version_id, author)
    audit(tx, author, "draft.create", artifact_id, vid, {"base": base_version_id})
    return get_version(tx, vid)


def diff(tx: Tx, a: str, b: str) -> dict[str, Any]:
    va, vb = get_version(tx, a), get_version(tx, b)
    from eap_metamodel import get_pointer

    changes = [{"path": p, "from": get_pointer(va["content"], p), "to": get_pointer(vb["content"], p)}
               for p in diff_paths(va["content"], vb["content"])]
    return {"from": a, "to": b, "changes": changes}


# ------------------------------------------------------------------------------ activation


def active_version(tx: Tx, artifact_id: str, environment: str) -> dict[str, Any] | None:
    r = tx.fetchone(
        "SELECT version_id FROM active_pointer WHERE artifact_id = ? AND environment = ? ORDER BY seq DESC LIMIT 1",
        (artifact_id, environment))
    return get_version(tx, r["version_id"]) if r else None


def pointer_history(tx: Tx, artifact_id: str, environment: str) -> list[dict[str, Any]]:
    return tx.fetchall("SELECT * FROM active_pointer WHERE artifact_id = ? AND environment = ? ORDER BY seq DESC",
                       (artifact_id, environment))


def insert_pointer(tx: Tx, artifact_id: str, environment: str, version_id: str, actor: str,
                   change_request_id: str | None) -> None:
    if environment not in ENVIRONMENTS:
        raise Invalid(f"unknown environment {environment!r}")
    tx.execute(
        "INSERT INTO active_pointer (id, artifact_id, environment, version_id, activated_by, activated_at, change_request_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id("ap_"), artifact_id, environment, version_id, actor, now_iso(), change_request_id))


def snapshot(tx: Tx, environment: str, overlay: dict[str, str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Active version of every artifact in `environment`; `overlay` maps artifact_id -> version_id."""
    overlay = dict(overlay or {})
    rows = tx.fetchall(
        "SELECT p.artifact_id, p.version_id FROM active_pointer p WHERE p.environment = ? AND p.seq = ("
        "SELECT MAX(q.seq) FROM active_pointer q WHERE q.artifact_id = p.artifact_id AND q.environment = p.environment)",
        (environment,))
    current = {r["artifact_id"]: r["version_id"] for r in rows}
    current.update(overlay)
    out: dict[str, list[dict[str, Any]]] = {}
    for aid, vid in current.items():
        art = get_artifact(tx, aid)
        v = get_version(tx, vid)
        out.setdefault(art["type"], []).append({"artifactId": aid, "name": art["name"], "versionId": vid,
                                                "version": v["version"], "content": v["content"]})
    return out


def dependents(tx: Tx, mm: Metamodel, artifact_id: str) -> list[dict[str, Any]]:
    """Artifacts whose latest version references `artifact_id` by name (C03-F5)."""
    target = get_artifact(tx, artifact_id)
    out = []
    for art in list_artifacts(tx):
        lv = latest_version(tx, art["id"])
        if not lv:
            continue
        refs = mm.references(art["type"], lv["content"])
        hit = [p for p, t, n in refs if t == target["type"] and n == target["name"]]
        # profiles and bindings point at agents through agentId
        if target["type"] == "agent" and lv["content"].get("agentId") == target["name"]:
            hit.append("/agentId")
        if hit:
            out.append({**art, "versionId": lv["id"], "paths": sorted(set(hit))})
    return out


# ------------------------------------------------------------------------------ roles


def grant(tx: Tx, principal: str, role: str, scope: str, granted_by: str) -> None:
    if not tx.fetchone("SELECT 1 AS x FROM role_binding WHERE principal = ? AND role = ? AND scope = ?",
                       (principal, role, scope)):
        tx.execute("INSERT INTO role_binding (principal, role, scope, granted_by, granted_at) VALUES (?, ?, ?, ?, ?)",
                   (principal, role, scope, granted_by, now_iso()))
    audit(tx, granted_by, "role.grant", principal, None, {"role": role, "scope": scope})


def roles_for(tx: Tx, principal: str, scopes: list[str]) -> set[str]:
    scopes = list({*scopes, "*"})
    marks = ",".join("?" for _ in scopes)
    rows = tx.fetchall(f"SELECT role FROM role_binding WHERE principal = ? AND scope IN ({marks})", [principal, *scopes])
    return {r["role"] for r in rows}


def principals_for(tx: Tx, role: str, scopes: list[str]) -> list[str]:
    scopes = list({*scopes, "*"})
    marks = ",".join("?" for _ in scopes)
    rows = tx.fetchall(f"SELECT DISTINCT principal FROM role_binding WHERE role = ? AND scope IN ({marks})",
                       [role, *scopes])
    return sorted(r["principal"] for r in rows)


def scopes_of(art: dict[str, Any], content: dict[str, Any]) -> list[str]:
    """Role scopes that apply to an artifact: its business unit and agent, if any."""
    s = []
    bu = content.get("businessUnit") or (content.get("scope") or {}).get("businessUnit")
    if bu:
        s.append(bu)
    agent = content.get("agentId") or (content.get("id") if art["type"] == "agent" else None)
    if agent:
        s.append(f"agent:{agent}")
    return s


# ------------------------------------------------------------------------------ runtime records


def record_deployment(tx: Tx, agent_version_id: str, profile_version_id: str | None, channel: str, platform: str,
                      environment: str, adapter_version: str, status: str, detail: dict[str, Any],
                      change_request_id: str | None) -> str:
    did = new_id("dp_")
    tx.execute(
        "INSERT INTO deployment (id, agent_version_id, profile_version_id, channel, platform, environment, "
        "adapter_version, status, detail, change_request_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (did, agent_version_id, profile_version_id, channel, platform, environment, adapter_version, status,
         dumps(detail), change_request_id, now_iso()))
    return did


def deployments(tx: Tx, environment: str | None = None) -> list[dict[str, Any]]:
    if environment:
        rows = tx.fetchall("SELECT * FROM deployment WHERE environment = ? ORDER BY seq DESC", (environment,))
    else:
        rows = tx.fetchall("SELECT * FROM deployment ORDER BY seq DESC")
    return [{**r, "detail": loads(r["detail"], {})} for r in rows]


def log_config_read(tx: Tx, agent_identity: str, versions: dict[str, str], environment: str, source: str) -> None:
    tx.execute(
        "INSERT INTO config_read_log (id, agent_identity, artifact_ids, version_ids, environment, source, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id("cr_"), agent_identity, dumps(sorted(versions)), dumps(versions), environment, source, now_iso()))
