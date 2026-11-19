"""C01 Agent metamodel: loader and rules over the JSON Schemas in /schemas/v1.

The JSON Schemas are the source of truth. This package adds what JSON Schema cannot express
on its own, read from the schemas' annotations:

  x-lane       "hot" (runtime-resolved) or "cold" (needs provisioning)     C01-F2
  x-editors    roles that may edit the field                              C01-F3
  x-approvers  roles that must approve a change to the field              C05-F2
  x-ref        artifact type a string field points at (dependencies)      C03-F5

Annotations inherit from the nearest ancestor that sets them.
"""

from .metamodel import (
    ARTIFACT_TYPES,
    ROLES,
    Classification,
    Metamodel,
    MetamodelError,
    diff_paths,
    get_pointer,
    load,
)
from .migrations import MIGRATIONS, migrate

__all__ = [
    "ARTIFACT_TYPES",
    "ROLES",
    "Classification",
    "Metamodel",
    "MetamodelError",
    "MIGRATIONS",
    "diff_paths",
    "get_pointer",
    "load",
    "migrate",
]
