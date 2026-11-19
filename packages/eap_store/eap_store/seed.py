"""Load the pilot data set (db/seed/pilot.json, spec 5.1) into an empty registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eap_core.db import Database, Tx
from eap_metamodel import Metamodel

from . import store

SEED_FILE = next(p / "db" / "seed" / "pilot.json" for p in Path(__file__).resolve().parents
                 if (p / "db" / "seed" / "pilot.json").is_file())

# Referenced artifacts must exist before the agent that points at them.
ORDER = ("contract", "role", "reasoning", "identity_ref", "model_policy", "guardrail_policy", "eval_suite",
         "skill", "tool_def", "knowledge_source", "agent", "profile", "binding")


def load_seed(path: str | Path | None = None) -> list[dict[str, Any]]:
    return json.loads(Path(path or SEED_FILE).read_text())["artifacts"]


async def seed(db: Database, mm: Metamodel, path: str | Path | None = None, author: str = "system:seed") -> list[str]:
    items = sorted(load_seed(path), key=lambda a: ORDER.index(a["type"]))

    def run(tx: Tx) -> list[str]:
        ids = []
        for a in items:
            if store.find_artifact(tx, a["type"], a["name"]):
                continue
            out = store.create_artifact(tx, mm, a["type"], a["name"], a["owner"], a["content"], author)
            ids.append(out["version"]["id"])
        return ids

    return await db.call(run)
