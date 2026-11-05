from __future__ import annotations

import os
from dataclasses import dataclass, field

from eap_core.auth import AuthConfig


@dataclass
class Settings:
    database_url: str = "sqlite://:memory:"
    runtime_url: str = "http://localhost:8081"
    runtime_auth: str = "dev"  # dev | google_id
    auth: AuthConfig = field(default_factory=AuthConfig)
    bootstrap_admins: list[str] = field(default_factory=list)
    seed_catalog: bool = True
    judge_model: str = "mock-small"
    internal_callers: list[str] = field(default_factory=lambda: ["runtime"])  # logical service names
    service_name: str = "governor"
    base_url: str = "http://localhost:8080"

    @staticmethod
    def from_env() -> "Settings":
        e = os.environ
        return Settings(
            database_url=e.get("DATABASE_URL", "sqlite:///governor.db"),
            runtime_url=e.get("EAP_RUNTIME_URL", "http://localhost:8081"),
            runtime_auth=e.get("EAP_RUNTIME_AUTH", "dev"),
            auth=AuthConfig.from_env(),
            bootstrap_admins=[a for a in e.get("EAP_BOOTSTRAP_ADMINS", "").split(",") if a],
            seed_catalog=e.get("EAP_SEED_CATALOG", "true").lower() == "true",
            judge_model=e.get("EAP_JUDGE_MODEL", "mock-small"),
            internal_callers=[c for c in e.get("EAP_INTERNAL_CALLERS", "runtime").split(",") if c],
            base_url=e.get("EAP_BASE_URL", "http://localhost:8080"),
        )
