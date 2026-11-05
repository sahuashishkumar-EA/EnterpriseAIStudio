from __future__ import annotations

import os
from dataclasses import dataclass, field

from eap_core.auth import AuthConfig


@dataclass
class Settings:
    database_url: str = "sqlite://:memory:"
    governor_url: str = "http://localhost:8080"
    governor_auth: str = "dev"  # dev | google_id
    auth: AuthConfig = field(default_factory=AuthConfig)
    call_context_key: str = "dev-only-call-context-key-change-me-0123456789"
    gcp_project: str | None = None
    resolver_ttl_s: float = 30.0
    resolver_max_stale_s: float = 600.0
    default_alias: str = "prod"
    mcp_alias: str = "prod"
    mcp_allowed_origins: list[str] = field(default_factory=list)
    # logical service names (see AuthConfig.service_accounts) allowed per internal endpoint
    execute_callers: list[str] = field(default_factory=lambda: ["governor"])
    event_callers: list[str] = field(default_factory=lambda: ["pubsub-push", "governor"])
    task_callers: list[str] = field(default_factory=lambda: ["runtime"])
    trusted_services: list[str] = field(default_factory=lambda: ["governor", "runtime"])
    # front-end services (e.g. the chat app) that authenticate end users themselves and may
    # invoke chat-exposed agents on their behalf via X-EAP-On-Behalf-Of
    delegating_services: list[str] = field(default_factory=lambda: ["chat"])
    web_ssrf_protection: bool = True
    service_name: str = "runtime"
    embedding_location: str = "europe-west4"

    @staticmethod
    def from_env() -> "Settings":
        e = os.environ

        def lst(name: str, default: str) -> list[str]:
            return [x.strip() for x in e.get(name, default).split(",") if x.strip()]

        return Settings(
            database_url=e.get("DATABASE_URL", "sqlite:///runtime.db"),
            governor_url=e.get("EAP_GOVERNOR_URL", "http://localhost:8080"),
            governor_auth=e.get("EAP_GOVERNOR_AUTH", "dev"),
            auth=AuthConfig.from_env(),
            call_context_key=e.get("EAP_CALL_CONTEXT_KEY", Settings.call_context_key),
            gcp_project=e.get("GOOGLE_CLOUD_PROJECT"),
            resolver_ttl_s=float(e.get("EAP_RESOLVER_TTL_S", "30")),
            resolver_max_stale_s=float(e.get("EAP_RESOLVER_MAX_STALE_S", "600")),
            default_alias=e.get("EAP_DEFAULT_ALIAS", "prod"),
            mcp_alias=e.get("EAP_MCP_ALIAS", "prod"),
            mcp_allowed_origins=lst("EAP_MCP_ALLOWED_ORIGINS", ""),
            execute_callers=lst("EAP_EXECUTE_CALLERS", "governor"),
            event_callers=lst("EAP_EVENT_CALLERS", "pubsub-push,governor"),
            task_callers=lst("EAP_TASK_CALLERS", "runtime"),
            trusted_services=lst("EAP_TRUSTED_SERVICES", "governor,runtime"),
            delegating_services=lst("EAP_DELEGATING_SERVICES", "chat"),
            embedding_location=e.get("EAP_EMBEDDING_LOCATION", "europe-west4"),
        )
