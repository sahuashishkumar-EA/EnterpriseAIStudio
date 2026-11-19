from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import jsonschema

# Order matters for display only.
ARTIFACT_TYPES = (
    "agent", "role", "contract", "reasoning", "identity_ref", "skill", "tool_def",
    "knowledge_source", "model_policy", "guardrail_policy", "eval_suite", "profile", "binding",
)
# Editing / approving roles (C01-F3). bu_owner approves BU-scoped hot changes.
ROLES = (
    "business_unit", "bu_owner", "business_owner", "domain_expert", "architect",
    "security", "risk", "platform", "qa",
)

_ANNOTATIONS = ("x-lane", "x-editors", "x-approvers", "x-ref")


class MetamodelError(Exception):
    def __init__(self, message: str, details: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.details = details or []


def _default_dir() -> Path:
    env = os.environ.get("EAP_SCHEMAS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "schemas" / "v1"
        if cand.is_dir():
            return cand
    raise MetamodelError("schemas/v1 not found; set EAP_SCHEMAS_DIR")


# ---------------------------------------------------------------------------- JSON helpers


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def get_pointer(doc: Any, pointer: str, default: Any = None) -> Any:
    if pointer in ("", "/"):
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        tok = _unescape(raw)
        if isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        elif isinstance(cur, list) and tok.isdigit() and int(tok) < len(cur):
            cur = cur[int(tok)]
        else:
            return default
    return cur


def diff_paths(old: Any, new: Any, base: str = "") -> list[str]:
    """Leaf JSON pointers whose value differs between `old` and `new` (added, removed or changed)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for k in sorted(set(old) | set(new)):
            p = f"{base}/{_escape(k)}"
            if k not in old or k not in new:
                out.append(p)
            else:
                out.extend(diff_paths(old[k], new[k], p))
        return out
    if isinstance(old, list) and isinstance(new, list):
        out = []
        for i in range(max(len(old), len(new))):
            p = f"{base}/{i}"
            if i >= len(old) or i >= len(new):
                out.append(p)
            else:
                out.extend(diff_paths(old[i], new[i], p))
        return out
    return [] if old == new else [base or "/"]


# ---------------------------------------------------------------------------- classification


@dataclass
class Classification:
    paths: list[str] = field(default_factory=list)
    hot: list[str] = field(default_factory=list)
    cold: list[str] = field(default_factory=list)
    approvers: set[str] = field(default_factory=set)
    editors: dict[str, list[str]] = field(default_factory=dict)  # path -> roles that may edit it

    @property
    def has_cold(self) -> bool:
        return bool(self.cold)


class Metamodel:
    def __init__(self, directory: str | os.PathLike | None = None):
        self.directory = Path(directory) if directory else _default_dir()
        self.schemas: dict[str, dict[str, Any]] = {}
        for f in sorted(self.directory.glob("*.schema.json")):
            s = json.loads(f.read_text())
            self.schemas[s["x-artifact-type"]] = s
        missing = set(ARTIFACT_TYPES) - set(self.schemas)
        if missing:
            raise MetamodelError(f"missing schemas: {sorted(missing)}")
        self._validators = {
            t: jsonschema.Draft202012Validator(s, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
            for t, s in self.schemas.items()
        }

    # -- schema access

    def schema(self, artifact_type: str) -> dict[str, Any]:
        try:
            return self.schemas[artifact_type]
        except KeyError:
            raise MetamodelError(f"unknown artifact type {artifact_type!r}") from None

    def schema_version(self, artifact_type: str) -> str:
        return self.schema(artifact_type)["x-schema-version"]

    # -- validation (C03-F2)

    def validate(self, artifact_type: str, content: Any) -> list[dict[str, Any]]:
        v = self._validators.get(artifact_type)
        if v is None:
            return [{"path": "/", "code": "unknown_type", "message": f"unknown artifact type {artifact_type!r}"}]
        errs = []
        for e in sorted(v.iter_errors(content), key=lambda e: list(e.absolute_path)):
            path = "/" + "/".join(_escape(str(p)) for p in e.absolute_path)
            errs.append({"path": path, "code": "schema", "message": e.message})
        return errs

    # -- annotations

    def annotations(self, artifact_type: str, pointer: str) -> dict[str, Any]:
        """Effective x-lane / x-editors / x-approvers / x-ref for a (possibly indexed) pointer."""
        node = self.schema(artifact_type)
        eff = {k: node[k] for k in _ANNOTATIONS if k in node and k != "x-ref"}
        for raw in [t for t in pointer.lstrip("/").split("/") if t]:
            tok = _unescape(raw)
            nxt = None
            if node.get("type") == "array" or "items" in node:
                if tok.isdigit():
                    nxt = node.get("items")
                else:
                    nxt = (node.get("items") or {}).get("properties", {}).get(tok)
            if nxt is None:
                props = node.get("properties") or {}
                if tok in props:
                    nxt = props[tok]
                elif isinstance(node.get("additionalProperties"), dict):
                    nxt = node["additionalProperties"]
            if nxt is None:
                break
            node = nxt
            for k in ("x-lane", "x-editors", "x-approvers"):
                if k in node:
                    eff[k] = node[k]
            # x-ref belongs to the exact field (or array items) only, never inherited
            ref = node.get("x-ref")
            if ref:
                eff["x-ref"] = ref
            else:
                eff.pop("x-ref", None)
        return eff

    def classify(self, artifact_type: str, old: Any, new: Any) -> Classification:
        """Hot / cold split and required approver roles for a change (C01-F2, C05-F2, C05-F6).

        A brand-new artifact (old is None) counts as structural: every changed path is cold
        unless the artifact type itself is hot by default (profiles, bindings that are hot)."""
        c = Classification()
        if old is None:
            paths = diff_paths({}, new)
        else:
            paths = diff_paths(old, new)
        c.paths = paths
        for p in paths:
            ann = self.annotations(artifact_type, p)
            lane = ann.get("x-lane", "cold")
            (c.hot if lane == "hot" else c.cold).append(p)
            c.approvers.update(ann.get("x-approvers", []))
            c.editors[p] = list(ann.get("x-editors", []))
        if old is None and artifact_type in ("agent", "tool_def", "knowledge_source", "identity_ref", "contract"):
            # New agents, tools, identities, indexes and contracts are provisioning-lane by definition.
            if not c.cold:
                c.cold = list(c.hot)
                c.hot = []
        return c

    def edit_violations(self, artifact_type: str, paths: Iterable[str], roles: set[str]) -> list[dict[str, Any]]:
        """Paths the caller's roles may not edit (C02-F2 / C03 non-functional)."""
        if "platform_admin" in roles:
            return []
        out = []
        for p in paths:
            editors = set(self.annotations(artifact_type, p).get("x-editors", []))
            if editors and not (editors & roles):
                out.append({"path": p, "code": "field_role", "message": f"requires one of {sorted(editors)}"})
        return out

    # -- references (C03-F5)

    def references(self, artifact_type: str, content: Any) -> list[tuple[str, str, str]]:
        """(pointer, referenced type, referenced artifact id) for every x-ref field in content."""
        out: list[tuple[str, str, str]] = []

        def walk(value: Any, pointer: str) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, f"{pointer}/{_escape(k)}")
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    walk(v, f"{pointer}/{i}")
            elif isinstance(value, str):
                ref = self.annotations(artifact_type, pointer).get("x-ref")
                if ref:
                    out.append((pointer, ref, value))

        walk(content, "")
        return out

    # -- profile limits (C01-F4)

    def limit_violations(self, artifact_type: str, content: dict[str, Any], limits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        key = artifact_type
        if artifact_type == "binding":
            key = f"binding:{content.get('bindingType')}"
        out = []
        for lim in limits or []:
            if lim.get("appliesTo") != key:
                continue
            path = lim["path"]
            value = get_pointer(content, path)
            if value is None:
                continue
            if "minimum" in lim and isinstance(value, (int, float)) and value < lim["minimum"]:
                out.append({"path": path, "code": "limit", "message": f"{value} is below the guardrail minimum {lim['minimum']}"})
            if "maximum" in lim and isinstance(value, (int, float)) and value > lim["maximum"]:
                out.append({"path": path, "code": "limit", "message": f"{value} exceeds the guardrail maximum {lim['maximum']}"})
            if "enum" in lim and value not in lim["enum"]:
                out.append({"path": path, "code": "limit", "message": f"{value!r} is not allowed by the guardrail policy"})
            if "maxLength" in lim and isinstance(value, str) and len(value) > lim["maxLength"]:
                out.append({"path": path, "code": "limit", "message": f"longer than the guardrail maximum {lim['maxLength']}"})
        return out


@lru_cache(maxsize=4)
def load(directory: str | None = None) -> Metamodel:
    return Metamodel(directory)
