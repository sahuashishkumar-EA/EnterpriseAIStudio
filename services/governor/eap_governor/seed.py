"""Initial catalog and platform policy.

Model availability on Vertex AI differs by region and changes often. Real models are
seeded as status 'preview' (not usable by published agents) until a platform admin has
verified the model ID and its availability in the chosen region and set it 'active'.
Prices are list-price placeholders and must be checked against Vertex AI pricing.
See docs/models.md.
"""

from __future__ import annotations

from eap_core.db import Tx, dumps
from eap_core.policy import PLATFORM_BASELINE, dump_policy

from . import store

VERIFY = "Verify model ID, regional availability and pricing for this location before activating."

MODELS = [
    {
        "id": "mock-small",
        "provider": "mock",
        "model_id": "mock-small",
        "location": "local",
        "status": "active",
        "capabilities": {"tools": True, "json": True, "context_tokens": 128000},
        "pricing": {"input_per_mtok": 0.10, "output_per_mtok": 0.40},
        "notes": "Deterministic local model for development and tests.",
    },
    {
        "id": "mock-large",
        "provider": "mock",
        "model_id": "mock-large",
        "location": "local",
        "status": "active",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 1.00, "output_per_mtok": 5.00},
        "notes": "Deterministic local model for development and tests.",
    },
    {
        "id": "gemini-2.5-flash-ew4",
        "provider": "vertex_gemini",
        "model_id": "gemini-2.5-flash",
        "location": "europe-west4",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 0.30, "output_per_mtok": 2.50},
        "notes": VERIFY,
    },
    {
        "id": "gemini-2.5-pro-ew4",
        "provider": "vertex_gemini",
        "model_id": "gemini-2.5-pro",
        "location": "europe-west4",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 1.25, "output_per_mtok": 10.00},
        "notes": VERIFY,
    },
    {
        "id": "google-ai-flash",
        "provider": "google_ai",
        "model_id": "gemini-2.5-flash",
        "location": "google-ai",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 0.30, "output_per_mtok": 2.50},
        "notes": "Gemini Developer API with the runtime's GOOGLE_AI_API_KEY. Click Test; if the model id is "
                 "unavailable, edit model_id (e.g. gemini-flash-latest). Data handling follows the Gemini API terms "
                 "(free tier may be used to improve Google products; no EU residency guarantee).",
    },
    {
        "id": "google-ai-pro",
        "provider": "google_ai",
        "model_id": "gemini-2.5-pro",
        "location": "google-ai",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 1.25, "output_per_mtok": 10.00},
        "notes": "Gemini Developer API (API key). Click Test before activating.",
    },
    {
        "id": "claude-opus-5-5-eu",
        "provider": "vertex_claude",
        "model_id": "claude-opus-5-5",
        "location": "europe-west4",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 4.00, "output_per_mtok": 20.00},
        "params": {"anthropic_version": "vertex-2023-10-16"},
        "notes": VERIFY + " Claude on Vertex may only be offered via the 'eu' multi-region or 'global' endpoint.",
    },
    {
        "id": "claude-sonnet-5-5-eu",
        "provider": "vertex_claude",
        "model_id": "claude-sonnet-5-5",
        "location": "europe-west4",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 2.00, "output_per_mtok": 10.00},
        "params": {"anthropic_version": "vertex-2023-10-16"},
        "notes": VERIFY,
    },
    {
        "id": "claude-haiku-5-5-eu",
        "provider": "vertex_claude",
        "model_id": "claude-haiku-5-5",
        "location": "europe-west4",
        "status": "preview",
        "capabilities": {"tools": True, "json": True, "context_tokens": 1000000},
        "pricing": {"input_per_mtok": 0.10, "output_per_mtok": 0.50},
        "params": {"anthropic_version": "vertex-2023-10-16"},
        "notes": VERIFY,
    },
]

TOOLS = [
    {"id": "echo", "kind": "builtin", "title": "Echo", "description": "Returns its input.",
     "config": {"function": "echo"}, "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
     "data_classification": "public"},
    {"id": "calculator", "kind": "builtin", "title": "Calculator", "description": "Evaluates arithmetic expressions.",
     "config": {"function": "calculator"},
     "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
     "data_classification": "public"},
    {"id": "current-time", "kind": "builtin", "title": "Current time", "description": "Current UTC time.",
     "config": {"function": "current_time"}, "input_schema": {"type": "object", "properties": {}},
     "data_classification": "public"},
    {"id": "crm-lookup", "kind": "builtin", "title": "CRM lookup (demo)",
     "description": "Looks up a customer record by customer_id in the demo CRM.",
     "config": {"function": "crm_lookup"},
     "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
     "data_classification": "confidential"},
]

KNOWLEDGE = [
    {"id": "customer-master", "kind": "pgvector", "title": "Customer master data (demo)",
     "description": "Golden customer records used for entity resolution.",
     "config": {"embedding_model": "mock-embedding", "dimensions": 768}, "data_classification": "confidential"},
    {"id": "product-docs", "kind": "pgvector", "title": "Product documentation (demo)",
     "description": "Public product documentation.",
     "config": {"embedding_model": "mock-embedding", "dimensions": 768}, "data_classification": "public"},
]


def seed(tx: Tx, actor: str = "system:seed") -> None:
    if not tx.fetchone("SELECT scope FROM policies WHERE scope = 'platform'"):
        tx.execute(
            "INSERT INTO policies (scope, document, revision, updated_by, updated_at) VALUES ('platform', ?, 1, ?, ?)",
            (dumps(dump_policy(PLATFORM_BASELINE)), actor, "1970-01-01T00:00:00Z"),
        )
    for kind, items in (("models", MODELS), ("tools", TOOLS), ("knowledge", KNOWLEDGE)):
        for item in items:
            table = store.CATALOG_TABLES[kind]
            if tx.fetchone(f"SELECT id FROM {table} WHERE id = ?", (item["id"],)):
                continue
            store.upsert_catalog(tx, kind, item["id"], {k: v for k, v in item.items() if k != "id"}, actor)
