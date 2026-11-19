"""Independent audit copy of every activated version in object storage (C03-F7).

GCP: a Cloud Storage bucket with Bucket Lock (retention policy, 7 years by default, spec 12).
Local: a directory, or memory in tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def object_name(artifact: dict[str, Any], version: dict[str, Any], environment: str) -> str:
    return f"{environment}/{artifact['type']}/{artifact['name']}/v{version['version']:06d}-{version['id']}.json"


def document(artifact: dict[str, Any], version: dict[str, Any], environment: str, change_request_id: str | None) -> bytes:
    return json.dumps({"artifact": artifact, "version": version, "environment": environment,
                       "changeRequestId": change_request_id}, sort_keys=True, indent=2).encode()


class MemoryExporter:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def export(self, artifact, version, environment, change_request_id=None) -> str:
        name = object_name(artifact, version, environment)
        self.objects[name] = document(artifact, version, environment, change_request_id)
        return name


class DirectoryExporter(MemoryExporter):
    def __init__(self, root: str) -> None:
        super().__init__()
        self.root = Path(root)

    async def export(self, artifact, version, environment, change_request_id=None) -> str:
        name = await super().export(artifact, version, environment, change_request_id)
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.objects[name])
        return name


class GcsExporter:
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    async def export(self, artifact, version, environment, change_request_id=None) -> str:
        from eap_core import gcp

        name = object_name(artifact, version, environment)
        await gcp.gcs_upload(self.bucket, name, document(artifact, version, environment, change_request_id),
                             "application/json")
        return name


def exporter_from_env():
    import os

    if os.environ.get("EAP_AUDIT_BUCKET"):
        return GcsExporter(os.environ["EAP_AUDIT_BUCKET"])
    if os.environ.get("EAP_AUDIT_DIR"):
        return DirectoryExporter(os.environ["EAP_AUDIT_DIR"])
    return MemoryExporter()
