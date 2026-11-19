"""One image, several Cloud Run services: EAP_SERVICE selects the component.

    registry   C03 Registry API (+ C05 workflow engine in-process for API calls)
    workflow   C05 checks + C11 provisioning workers (Cloud Tasks push target), runs C06 and C10 in-process
    resolver   C07 profile resolver (runtime path)
    config-mcp C08 config MCP server (runtime path)

Everything is configured from the environment; see infra/terraform/config_platform.tf.
"""

from __future__ import annotations

import os


def _plane():
    """Configuration-plane objects shared by registry and workflow."""
    from eap_compiler.adapters import MockAdapter
    from eap_compiler.secrets import secrets_from_env
    from eap_core.db import database_from_env
    from eap_core.events import publisher_from_env
    from eap_core.tasks import queue_from_env
    import eap_metamodel
    from eap_evalrunner import EvalRunner
    from eap_store import MIGRATIONS_DIR, export
    from eap_workflow import PromotionEngine, Provisioner, WorkflowSettings, policy_from_env

    db = database_from_env()
    with db.startup_lock("registry-migrations"):
        db.migrate(MIGRATIONS_DIR)
    mm = eap_metamodel.load()
    tiers = {t for t in os.environ.get("EAP_AUTO_APPROVE_TIERS", "").split(",") if t}
    engine = PromotionEngine(db, mm, EvalRunner(db, mm), publisher_from_env(), export.exporter_from_env(),
                             policy_from_env(), queue_from_env() if os.environ.get("EAP_TASKS_QUEUE") else None,
                             WorkflowSettings(auto_approval_enabled=bool(tiers), auto_approve_risk_tiers=tiers,
                                              allow_bootstrap=os.environ.get("EAP_ALLOW_BOOTSTRAP", "true") == "true"))
    adapter_id = os.environ.get("EAP_ADAPTER", "mock")
    if adapter_id == "mock":
        adapter = MockAdapter(secrets_from_env(), state_file=os.environ.get("EAP_MOCK_STATE_FILE"))
    else:  # pragma: no cover - ADR-002
        from eap_compiler.adapters.agent_platform import AgentPlatformAdapter

        adapter = AgentPlatformAdapter(os.environ["GOOGLE_CLOUD_PROJECT"], os.environ.get("EAP_PLATFORM_LOCATION", "europe-west4"))
    return db, mm, engine, Provisioner(engine, adapter)


def build():
    service = os.environ.get("EAP_SERVICE", "registry")
    internal = {p for p in os.environ.get("EAP_INTERNAL_PRINCIPALS", "").split(",") if p} or None
    if service in ("registry", "workflow"):
        from eap_registry import create_app

        db, mm, engine, prov = _plane()
        return create_app(db, mm, engine, provisioner=prov, internal_principals=internal)
    if service == "resolver":
        from eap_resolver.app import create_app
        from eap_resolver.core import AgentIdentities, FileLkg, HttpSource, MemoryCache, Resolver, ValkeyCache

        cache = ValkeyCache(os.environ["EAP_VALKEY_URL"]) if os.environ.get("EAP_VALKEY_URL") else MemoryCache()

        async def headers():
            if os.environ.get("EAP_REGISTRY_AUTH", "google_id") == "dev":
                return {"X-Dev-User": "resolver", "X-Dev-Kind": "service"}
            from eap_core import gcp

            return {"Authorization": f"Bearer {await gcp.id_token(os.environ['EAP_REGISTRY_URL'])}"}

        r = Resolver(HttpSource(os.environ["EAP_REGISTRY_URL"], headers), cache,
                     FileLkg(os.environ.get("EAP_LKG_DIR", "/tmp/eap-lkg")), AgentIdentities.from_env())
        return create_app(r)
    if service == "config-mcp":
        raise SystemExit("config-mcp: Phase 2; see services/config-mcp")
    raise SystemExit(f"unknown EAP_SERVICE {service!r}")


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()
