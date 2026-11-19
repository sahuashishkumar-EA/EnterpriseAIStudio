"""Schema migrations between metamodel versions (C01-F5).

Stored versions are immutable, so migrations upcast on read: the Registry API returns content
at the current schema version, and the stored row (with its hash) is never touched. A migration
must be lossless: every value in the old document appears in the new one.

Register a migration as MIGRATIONS[(artifact_type, from_version)] = (to_version, fn).
"""

from __future__ import annotations

import copy
from typing import Any, Callable

Migration = Callable[[dict[str, Any]], dict[str, Any]]


def _profile_1_0_to_1_1(doc: dict[str, Any]) -> dict[str, Any]:
    # 1.0.0 kept `tone` at the top level and `persona` as a plain string.
    out = copy.deepcopy(doc)
    persona = out.pop("persona", None)
    tone = out.pop("tone", "professional")
    if isinstance(persona, str):
        out["persona"] = {"name": persona, "tone": tone}
    elif isinstance(persona, dict):
        out["persona"] = {**persona, "tone": persona.get("tone", tone)}
    else:
        out["persona"] = {"name": "Assistant", "tone": tone}
    out.setdefault("escalationContacts", [])
    return out


MIGRATIONS: dict[tuple[str, str], tuple[str, Migration]] = {
    ("profile", "1.0.0"): ("1.1.0", _profile_1_0_to_1_1),
}


def migrate(artifact_type: str, content: dict[str, Any], from_version: str, to_version: str) -> dict[str, Any]:
    version, doc = from_version, content
    seen = set()
    while version != to_version:
        step = MIGRATIONS.get((artifact_type, version))
        if step is None or version in seen:
            raise ValueError(f"no migration path for {artifact_type} {from_version} -> {to_version}")
        seen.add(version)
        version, fn = step[0], step[1]
        doc = fn(doc)
    return doc
