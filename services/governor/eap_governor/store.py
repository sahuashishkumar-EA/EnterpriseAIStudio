"""Registry storage: agents, drafts, immutable versions, aliases (+ history, canary),
dependency graph, catalog, policies, audit, and the runtime resolve bundle.

All functions are synchronous and take an open transaction (`Tx`); the HTTP layer runs
them on a worker thread via `Database.call`.
"""

from __future__ import annotations

from typing import Any

from eap_core import schema
from eap_core.db import Tx, dumps, loads, new_id, now_iso
from eap_core.policy import (
    PLATFORM_BASELINE,
    Policy,
    PolicyOverlay,
    dump_policy,
    effective_guardrails,
    effective_limits,
    merge,
    validate_overlay,
)


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class Invalid(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


# ------------------------------------------------------------------------- audit


def audit(tx: Tx, actor: str, action: str, target: str, details: dict[str, Any] | None = None) -> None:
    tx.execute(
        "INSERT INTO audit_log (id, at, actor, action, target, details) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id("aud_"), now_iso(), actor, action, target, dumps(details or {})),
    )


def list_audit(tx: Tx, target: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if target:
        rows = tx.fetchall(
            "SELECT * FROM audit_log WHERE target = ? OR target LIKE ? ORDER BY at DESC, id DESC LIMIT ?",
            (target, target + "/%", limit),
        )
    else:
        rows = tx.fetchall("SELECT * FROM audit_log ORDER BY at DESC, id DESC LIMIT ?", (limit,))
    for r in rows:
        r["details"] = loads(r["details"], {})
    return rows


# ------------------------------------------------------------------------ agents


def get_agent(tx: Tx, name: str) -> dict[str, Any]:
    row = tx.fetchone("SELECT * FROM agents WHERE name = ?", (name,))
    if row is None:
        raise NotFound(f"agent {name} not found")
    return row


def list_agents(tx: Tx, bus: set[str] | None = None) -> list[dict[str, Any]]:
    rows = tx.fetchall(
        "SELECT a.*, d.content_hash AS draft_hash, d.updated_at AS draft_updated_at FROM agents a "
        "LEFT JOIN drafts d ON d.agent_name = a.name WHERE a.archived = 0 ORDER BY a.name"
    )
    if bus is not None:
        rows = [r for r in rows if r["owner_bu"] in bus]
    for r in rows:
        r["latest_version"] = latest_version(tx, r["name"])
        r["aliases"] = {a["alias"]: a["version"] for a in list_aliases(tx, r["name"])}
    return rows


def create_agent(tx: Tx, name: str, owner_bu: str, description: str, actor: str, definition: dict | None) -> None:
    if not schema.NAME_RE.match(name):
        raise Invalid("invalid agent name (lowercase letters, digits, dashes)")
    if tx.fetchone("SELECT name FROM agents WHERE name = ?", (name,)):
        raise Conflict(f"agent {name} already exists")
    tx.execute(
        "INSERT INTO agents (name, owner_bu, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, owner_bu, description, actor, now_iso()),
    )
    definition = definition or {
        "name": name,
        "owner_bu": owner_bu,
        "description": description,
        "prompt": {"system": f"You are {name}."},
        "model": {"catalog_id": "mock-small"},
    }
    save_draft(tx, name, definition, actor)
    audit(tx, actor, "agent.create", f"agent/{name}", {"owner_bu": owner_bu})


# ------------------------------------------------------------------------ drafts


def parse_or_invalid(data: dict[str, Any]) -> schema.AgentDefinition:
    try:
        return schema.parse_definition(data)
    except Exception as e:  # pydantic.ValidationError
        errors = getattr(e, "errors", None)
        details = (
            [{"path": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in errors()] if errors else str(e)
        )
        raise Invalid("definition does not match schema v1", details) from e


def save_draft(tx: Tx, name: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
    agent = get_agent(tx, name)
    defn = parse_or_invalid(data)
    if defn.name != name:
        raise Invalid("definition.name must equal the agent name")
    if defn.owner_bu != agent["owner_bu"]:
        raise Invalid("definition.owner_bu must equal the agent's owner BU")
    h = defn.content_hash()
    cur = tx.fetchone("SELECT * FROM drafts WHERE agent_name = ?", (name,))
    if cur is None:
        tx.execute(
            "INSERT INTO drafts (agent_name, definition, content_hash, updated_by, updated_at, editors) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, dumps(defn.canonical()), h, actor, now_iso(), dumps([actor])),
        )
    elif cur["content_hash"] != h:
        editors = loads(cur["editors"], [])
        if actor not in editors:
            editors.append(actor)
        tx.execute(
            "UPDATE drafts SET definition = ?, content_hash = ?, updated_by = ?, updated_at = ?, editors = ? "
            "WHERE agent_name = ?",
            (dumps(defn.canonical()), h, actor, now_iso(), dumps(editors), name),
        )
        audit(tx, actor, "draft.update", f"agent/{name}", {"content_hash": h, "previous_hash": cur["content_hash"]})
    return get_draft(tx, name)


def get_draft(tx: Tx, name: str) -> dict[str, Any]:
    row = tx.fetchone("SELECT * FROM drafts WHERE agent_name = ?", (name,))
    if row is None:
        raise NotFound(f"agent {name} has no draft")
    row["definition"] = loads(row["definition"])
    row["editors"] = loads(row["editors"], [])
    return row


# ---------------------------------------------------------------------- versions


def latest_version(tx: Tx, name: str) -> str | None:
    row = tx.fetchone(
        "SELECT version FROM versions WHERE agent_name = ? ORDER BY major DESC, minor DESC, patch DESC LIMIT 1",
        (name,),
    )
    return row["version"] if row else None


def _version_row(r: dict[str, Any]) -> dict[str, Any]:
    r["definition"] = loads(r["definition"])
    for k in ("breaking", "eval_run_ids", "approvals"):
        r[k] = loads(r[k], [])
    return r


def get_version(tx: Tx, name: str, version: str) -> dict[str, Any]:
    row = tx.fetchone("SELECT * FROM versions WHERE agent_name = ? AND version = ?", (name, version))
    if row is None:
        raise NotFound(f"{name}@{version} not found")
    return _version_row(row)


def list_versions(tx: Tx, name: str) -> list[dict[str, Any]]:
    rows = tx.fetchall(
        "SELECT agent_name, version, content_hash, changelog, breaking, published_by, published_at, status, "
        "flag_reason FROM versions WHERE agent_name = ? ORDER BY major DESC, minor DESC, patch DESC",
        (name,),
    )
    for r in rows:
        r["breaking"] = loads(r["breaking"], [])
    return rows


def insert_version(
    tx: Tx,
    defn: schema.AgentDefinition,
    version: str,
    actor: str,
    changelog: str,
    breaking: list[str],
    eval_run_ids: list[str],
    approvals: list[str],
) -> None:
    ma, mi, pa = schema.parse_semver(version)
    tx.execute(
        "INSERT INTO versions (agent_name, version, major, minor, patch, definition, content_hash, changelog, "
        "breaking, published_by, published_at, eval_run_ids, approvals) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            defn.name,
            version,
            ma,
            mi,
            pa,
            dumps(defn.canonical()),
            defn.content_hash(),
            changelog,
            dumps(breaking),
            actor,
            now_iso(),
            dumps(eval_run_ids),
            dumps(approvals),
        ),
    )
    for kind, ref, alias in dependency_edges(defn):
        tx.execute(
            "INSERT INTO dependencies (agent_name, version, dep_kind, dep_ref, dep_alias) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (defn.name, version, kind, ref, alias),
        )


def dependency_edges(defn: schema.AgentDefinition) -> list[tuple[str, str, str | None]]:
    edges: list[tuple[str, str, str | None]] = [("model", defn.model.catalog_id, None)]
    edges += [("model", m, None) for m in defn.model.fallbacks]
    edges += [("tool", t.ref, None) for t in defn.bindings.tools]
    edges += [("knowledge", k.ref, None) for k in defn.bindings.knowledge]
    edges += [("agent", a.name, a.alias) for a in defn.bindings.agents]
    return list(dict.fromkeys(edges))


def flag_version(tx: Tx, name: str, version: str, reason: str, actor: str) -> None:
    tx.execute(
        "UPDATE versions SET status = 'flagged', flag_reason = ? WHERE agent_name = ? AND version = ?",
        (reason, name, version),
    )
    audit(tx, actor, "version.flag", f"agent/{name}/{version}", {"reason": reason})


# ----------------------------------------------------------------------- aliases


def list_aliases(tx: Tx, name: str) -> list[dict[str, Any]]:
    return tx.fetchall("SELECT * FROM aliases WHERE agent_name = ? ORDER BY alias", (name,))


def get_alias(tx: Tx, name: str, alias: str) -> dict[str, Any] | None:
    return tx.fetchone("SELECT * FROM aliases WHERE agent_name = ? AND alias = ?", (name, alias))


def move_alias(
    tx: Tx,
    name: str,
    alias: str,
    version: str,
    actor: str,
    *,
    canary_version: str | None = None,
    canary_weight: int = 0,
    reason: str = "",
    allow_flagged: bool = False,
) -> dict[str, Any]:
    if not schema.NAME_RE.match(alias) and alias not in ("prod", "staging", "dev"):
        raise Invalid("invalid alias name")
    if schema.SEMVER_RE.match(alias):
        raise Invalid("alias may not look like a version")
    for v in filter(None, [version, canary_version]):
        row = get_version(tx, name, v)
        if row["status"] != "active" and not allow_flagged:
            raise Conflict(f"{name}@{v} is {row['status']} ({row['flag_reason']}); pass allow_flagged to override")
    if not 0 <= int(canary_weight) <= 100:
        raise Invalid("canary_weight must be 0..100")
    if canary_version is None:
        canary_weight = 0
    if canary_version == version:
        canary_version, canary_weight = None, 0
    cur = get_alias(tx, name, alias)
    ts = now_iso()
    if cur is None:
        tx.execute(
            "INSERT INTO aliases (agent_name, alias, version, canary_version, canary_weight, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, alias, version, canary_version, canary_weight, actor, ts),
        )
    else:
        tx.execute(
            "UPDATE aliases SET version = ?, canary_version = ?, canary_weight = ?, updated_by = ?, updated_at = ? "
            "WHERE agent_name = ? AND alias = ?",
            (version, canary_version, canary_weight, actor, ts, name, alias),
        )
    tx.execute(
        "INSERT INTO alias_history (id, agent_name, alias, version, canary_version, canary_weight, previous_version, "
        "actor, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("ah_"),
            name,
            alias,
            version,
            canary_version,
            canary_weight,
            cur["version"] if cur else None,
            actor,
            reason,
            ts,
        ),
    )
    audit(
        tx,
        actor,
        "alias.move",
        f"agent/{name}/alias/{alias}",
        {"version": version, "previous": cur["version"] if cur else None, "canary_version": canary_version,
         "canary_weight": canary_weight, "reason": reason},
    )
    return get_alias(tx, name, alias)


def alias_history(tx: Tx, name: str, alias: str) -> list[dict[str, Any]]:
    return tx.fetchall(
        "SELECT * FROM alias_history WHERE agent_name = ? AND alias = ? ORDER BY at DESC, id DESC", (name, alias)
    )


def rollback_alias(tx: Tx, name: str, alias: str, actor: str, reason: str) -> dict[str, Any]:
    """Point the alias at the version it had before the current one (canary cleared)."""
    cur = get_alias(tx, name, alias)
    if cur is None:
        raise NotFound(f"alias {alias} not found")
    history = alias_history(tx, name, alias)
    target = None
    for h in history:
        if h["version"] != cur["version"]:
            target = h["version"]
            break
    if target is None:
        raise Conflict("no previous version to roll back to")
    return move_alias(tx, name, alias, target, actor, reason=reason or "rollback", allow_flagged=True)


# ----------------------------------------------------------------------- catalog

CATALOG_TABLES = {"models": "catalog_models", "tools": "catalog_tools", "knowledge": "catalog_knowledge"}
CATALOG_JSON = {"capabilities", "pricing", "params", "config", "input_schema"}
CATALOG_FIELDS = {
    "models": ("provider", "model_id", "location", "status", "capabilities", "pricing", "params", "notes"),
    "tools": ("kind", "title", "description", "config", "input_schema", "data_classification", "owner_bu", "status"),
    "knowledge": ("kind", "title", "description", "config", "data_classification", "owner_bu", "status"),
}


def _cat_row(r: dict[str, Any]) -> dict[str, Any]:
    for k in CATALOG_JSON & set(r):
        r[k] = loads(r[k], {})
    return r


def list_catalog(tx: Tx, kind: str) -> list[dict[str, Any]]:
    return [_cat_row(r) for r in tx.fetchall(f"SELECT * FROM {CATALOG_TABLES[kind]} ORDER BY id")]


def get_catalog(tx: Tx, kind: str, id_: str) -> dict[str, Any]:
    r = tx.fetchone(f"SELECT * FROM {CATALOG_TABLES[kind]} WHERE id = ?", (id_,))
    if r is None:
        raise NotFound(f"{kind} {id_} not found")
    return _cat_row(r)


def upsert_catalog(tx: Tx, kind: str, id_: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
    from eap_core import plugins

    fields = CATALOG_FIELDS[kind]
    unknown = set(data) - set(fields) - {"id"}
    if unknown:
        raise Invalid(f"unknown fields {sorted(unknown)}")
    if kind == "models":
        prov = data.get("provider")
        if plugins.get("model_provider", prov or "") is None:
            raise Invalid(f"unknown model provider {prov}")
        if data.get("status", "active") not in ("active", "preview", "deprecated", "retired", "blocked"):
            raise Invalid("bad model status")
    elif kind == "tools":
        errs = plugins.validate_params("tool", data.get("kind", ""), data.get("config", {}))
        if errs:
            raise Invalid("invalid tool config", errs)
    elif kind == "knowledge":
        errs = plugins.validate_params("knowledge", data.get("kind", ""), data.get("config", {}))
        if errs:
            raise Invalid("invalid knowledge config", errs)
    table = CATALOG_TABLES[kind]
    cols = [f for f in fields if f in data]
    vals = [dumps(data[f]) if f in CATALOG_JSON else data[f] for f in cols]
    existing = tx.fetchone(f"SELECT id FROM {table} WHERE id = ?", (id_,))
    if existing:
        sets = ", ".join(f"{c} = ?" for c in cols + ["updated_by", "updated_at"])
        tx.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*vals, actor, now_iso(), id_))
    else:
        all_cols = ["id", *cols, "updated_by", "updated_at"]
        ph = ", ".join("?" for _ in all_cols)
        tx.execute(f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES ({ph})", (id_, *vals, actor, now_iso()))
    audit(tx, actor, f"catalog.{kind}.upsert", f"catalog/{kind}/{id_}", {k: data[k] for k in cols if k != "config"})
    return get_catalog(tx, kind, id_)


def set_model_status(tx: Tx, model_id: str, status: str, actor: str, reason: str = "") -> list[tuple[str, str]]:
    """Change a model's status; deprecated/retired/blocked flags every active version using it."""
    get_catalog(tx, "models", model_id)
    if status not in ("active", "preview", "deprecated", "retired", "blocked"):
        raise Invalid("bad model status")
    tx.execute(
        "UPDATE catalog_models SET status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
        (status, actor, now_iso(), model_id),
    )
    audit(tx, actor, "catalog.models.status", f"catalog/models/{model_id}", {"status": status, "reason": reason})
    flagged: list[tuple[str, str]] = []
    if status in ("deprecated", "retired", "blocked"):
        rows = tx.fetchall(
            "SELECT DISTINCT v.agent_name, v.version FROM dependencies d JOIN versions v "
            "ON v.agent_name = d.agent_name AND v.version = d.version "
            "WHERE d.dep_kind = 'model' AND d.dep_ref = ? AND v.status = 'active'",
            (model_id,),
        )
        for r in rows:
            flag_version(tx, r["agent_name"], r["version"], f"model {model_id} is {status}", actor)
            flagged.append((r["agent_name"], r["version"]))
    return flagged


def catalog_subset(tx: Tx, defn: schema.AgentDefinition) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {"models": {}, "tools": {}, "knowledge": {}}
    for mid in [defn.model.catalog_id, *defn.model.fallbacks]:
        r = tx.fetchone("SELECT * FROM catalog_models WHERE id = ?", (mid,))
        if r:
            out["models"][mid] = _cat_row(r)
    for t in defn.bindings.tools:
        r = tx.fetchone("SELECT * FROM catalog_tools WHERE id = ?", (t.ref,))
        if r:
            out["tools"][t.ref] = _cat_row(r)
    for k in defn.bindings.knowledge:
        r = tx.fetchone("SELECT * FROM catalog_knowledge WHERE id = ?", (k.ref,))
        if r:
            out["knowledge"][k.ref] = _cat_row(r)
    return out


# ---------------------------------------------------------------------- policies


def get_platform_policy(tx: Tx) -> Policy:
    r = tx.fetchone("SELECT document FROM policies WHERE scope = 'platform'")
    return Policy.model_validate(loads(r["document"])) if r else PLATFORM_BASELINE


def get_bu_overlay(tx: Tx, bu: str) -> PolicyOverlay | None:
    r = tx.fetchone("SELECT document FROM policies WHERE scope = ?", (f"bu:{bu}",))
    return PolicyOverlay.model_validate(loads(r["document"])) if r else None


def effective_policy(tx: Tx, bu: str) -> Policy:
    return merge(get_platform_policy(tx), get_bu_overlay(tx, bu))


def _put_policy(tx: Tx, scope: str, doc: dict[str, Any], actor: str) -> int:
    cur = tx.fetchone("SELECT revision FROM policies WHERE scope = ?", (scope,))
    if cur:
        rev = cur["revision"] + 1
        tx.execute(
            "UPDATE policies SET document = ?, revision = ?, updated_by = ?, updated_at = ? WHERE scope = ?",
            (dumps(doc), rev, actor, now_iso(), scope),
        )
    else:
        rev = 1
        tx.execute(
            "INSERT INTO policies (scope, document, revision, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
            (scope, dumps(doc), rev, actor, now_iso()),
        )
    audit(tx, actor, "policy.update", f"policy/{scope}", {"revision": rev})
    return rev


def put_platform_policy(tx: Tx, doc: dict[str, Any], actor: str) -> Policy:
    try:
        p = Policy.model_validate(doc)
    except Exception as e:
        raise Invalid("invalid platform policy", str(e)) from e
    _put_policy(tx, "platform", dump_policy(p), actor)
    return p


def put_bu_overlay(tx: Tx, bu: str, doc: dict[str, Any], actor: str) -> PolicyOverlay:
    try:
        o = PolicyOverlay.model_validate(doc)
    except Exception as e:
        raise Invalid("invalid policy overlay", str(e)) from e
    errs = validate_overlay(get_platform_policy(tx), o)
    if errs:
        raise Invalid("BU policy may only tighten platform policy", errs)
    _put_policy(tx, f"bu:{bu}", dump_policy(o), actor)
    return o


# ------------------------------------------------------------------ graph & cycles


def graph(tx: Tx, agent: str | None = None) -> dict[str, Any]:
    """Dependency graph of the versions currently referenced by aliases (and canaries)."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    live = tx.fetchall("SELECT agent_name, alias, version, canary_version FROM aliases")
    pairs = {(r["agent_name"], r["version"]) for r in live} | {
        (r["agent_name"], r["canary_version"]) for r in live if r["canary_version"]
    }
    for name, version in sorted(pairs):
        key = f"agent:{name}@{version}"
        nodes[key] = {"id": key, "kind": "agent", "name": name, "version": version,
                      "aliases": [r["alias"] for r in live if r["agent_name"] == name and r["version"] == version]}
        for d in tx.fetchall(
            "SELECT dep_kind, dep_ref, dep_alias FROM dependencies WHERE agent_name = ? AND version = ?",
            (name, version),
        ):
            target = f"{d['dep_kind']}:{d['dep_ref']}"
            if d["dep_kind"] == "agent":
                a = get_alias(tx, d["dep_ref"], d["dep_alias"] or "prod")
                target = f"agent:{d['dep_ref']}@{a['version']}" if a else f"agent:{d['dep_ref']}@<unresolved>"
            nodes.setdefault(target, {"id": target, "kind": d["dep_kind"], "name": d["dep_ref"]})
            edges.append({"from": key, "to": target, "kind": d["dep_kind"], "alias": d["dep_alias"]})
    if agent:
        keep = {k for k in nodes if nodes[k].get("name") == agent}
        changed = True
        while changed:
            changed = False
            for e in edges:
                if (e["from"] in keep) != (e["to"] in keep) and (e["from"] in keep or e["to"] in keep):
                    keep |= {e["from"], e["to"]}
                    changed = True
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    return {"nodes": list(nodes.values()), "edges": edges}


def find_static_cycle(tx: Tx, defn: schema.AgentDefinition) -> list[str] | None:
    """Would publishing `defn` create an agent-call cycle through currently aliased versions?"""
    root = defn.name

    def deps_of(name: str, alias: str) -> list[tuple[str, str]]:
        a = get_alias(tx, name, alias) if not schema.SEMVER_RE.match(alias) else {"version": alias,
                                                                                    "canary_version": None}
        if a is None:
            return []
        out: list[tuple[str, str]] = []
        for v in filter(None, [a["version"], a.get("canary_version")]):
            out += [
                (r["dep_ref"], r["dep_alias"] or "prod")
                for r in tx.fetchall(
                    "SELECT dep_ref, dep_alias FROM dependencies WHERE agent_name = ? AND version = ? "
                    "AND dep_kind = 'agent'",
                    (name, v),
                )
            ]
        return out

    stack: list[tuple[str, list[str]]] = [(b.name, [root, b.name]) for b in defn.bindings.agents]
    alias_of = {b.name: b.alias for b in defn.bindings.agents}
    seen: set[str] = set()
    while stack:
        name, path = stack.pop()
        if name == root:
            return path
        if name in seen:
            continue
        seen.add(name)
        for dep, dep_alias in deps_of(name, alias_of.get(name, "prod")):
            alias_of.setdefault(dep, dep_alias)
            stack.append((dep, [*path, dep]))
    return None


# ----------------------------------------------------------------------- resolve


def release_payload(tx: Tx, defn: schema.AgentDefinition, policy: Policy, **extra: Any) -> dict[str, Any]:
    """What the runtime needs to execute one definition, without reading governor tables."""
    return {
        **extra,
        "content_hash": defn.content_hash(),
        "definition": defn.canonical(),
        "limits": effective_limits(defn, policy),
        "guardrails": [g.model_dump(mode="json") for g in effective_guardrails(defn, policy)],
        "soft_controls": list(policy.soft_controls),
        "catalog": catalog_subset(tx, defn),
    }


def resolve(tx: Tx, name: str, alias: str) -> dict[str, Any]:
    agent = get_agent(tx, name)
    if schema.SEMVER_RE.match(alias):
        a = {"version": alias, "canary_version": None, "canary_weight": 0}
    else:
        a = get_alias(tx, name, alias)
        if a is None:
            raise NotFound(f"alias {name}@{alias} not found")
    policy = effective_policy(tx, agent["owner_bu"])

    def rel(version: str) -> dict[str, Any]:
        v = get_version(tx, name, version)
        defn = schema.parse_definition(v["definition"])
        return release_payload(tx, defn, policy, version=version, status=v["status"], flag_reason=v["flag_reason"])

    bundle = {
        "agent": name,
        "alias": alias,
        "owner_bu": agent["owner_bu"],
        "policy": dump_policy(policy),
        "primary": rel(a["version"]),
        "canary": rel(a["canary_version"]) if a.get("canary_version") else None,
        "canary_weight": a.get("canary_weight", 0) if a.get("canary_version") else 0,
    }
    bundle["etag"] = '"' + schema.content_hash(bundle).split(":", 1)[1][:32] + '"'
    return bundle


def mcp_exposed(tx: Tx, alias: str) -> list[dict[str, Any]]:
    out = []
    for a in tx.fetchall("SELECT agent_name, version FROM aliases WHERE alias = ?", (alias,)):
        v = get_version(tx, a["agent_name"], a["version"])
        d = v["definition"]
        if d.get("exposure", {}).get("mcp"):
            out.append(
                {
                    "name": a["agent_name"],
                    "alias": alias,
                    "version": a["version"],
                    "description": d.get("description") or d.get("contract", {}).get("description", ""),
                    "input_schema": d.get("contract", {}).get("input_schema") or {"type": "object"},
                }
            )
    return out
