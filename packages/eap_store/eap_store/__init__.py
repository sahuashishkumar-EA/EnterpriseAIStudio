"""Shared data access for the configuration-plane services (C03 registry-api, C05/C11 workflow,
C06 eval-runner) over the C04 registry database. Runtime-path services never import this:
they read through the resolver (C07)."""

from pathlib import Path

from . import store

MIGRATIONS_DIR = next(p / "db" / "migrations" for p in Path(__file__).resolve().parents if (p / "db" / "migrations").is_dir())

__all__ = ["store", "MIGRATIONS_DIR"]
