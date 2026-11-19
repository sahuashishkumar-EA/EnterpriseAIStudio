"""Platform credentials are read from the secrets vault at run time, never from configuration (C10-F6).

GCP: Secret Manager. Local: environment variables EAP_SECRET_<NAME> or an env file (spec 2.3 stand-in).
"""

from __future__ import annotations

import os
from pathlib import Path


class SecretNotFound(Exception):
    pass


class EnvFileSecrets:
    def __init__(self, path: str | None = None):
        self.values: dict[str, str] = {}
        path = path or os.environ.get("EAP_SECRETS_FILE")
        if path and Path(path).is_file():
            for line in Path(path).read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    self.values[k.strip()] = v.strip()

    async def get(self, name: str) -> str:
        env = os.environ.get("EAP_SECRET_" + name.upper().replace("-", "_"))
        if env:
            return env
        if name in self.values:
            return self.values[name]
        raise SecretNotFound(name)


class SecretManagerSecrets:
    def __init__(self, project: str):
        self.project = project

    async def get(self, name: str) -> str:
        from eap_core import gcp

        return await gcp.secret_access(f"projects/{self.project}/secrets/{name}/versions/latest")


def secrets_from_env():
    project = os.environ.get("EAP_SECRETS_PROJECT")
    return SecretManagerSecrets(project) if project else EnvFileSecrets()
