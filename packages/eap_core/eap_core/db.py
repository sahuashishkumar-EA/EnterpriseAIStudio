"""Portable SQL access: SQLite locally, Cloud SQL Postgres (psycopg3) in GCP.

SQL is written once with `?` placeholders and portable DDL (TEXT / INTEGER / BIGINT /
DOUBLE PRECISION, ISO-8601 timestamps as TEXT, JSON as TEXT). Dialect-only migrations use
the suffixes `.pg.sql` / `.sqlite.sql` (e.g. pgvector).

The API is synchronous; async callers use `await db.call(fn, ...)`, which runs a function
on a worker thread with a connection inside one transaction.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import anyio


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def dumps(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def loads(v: str | None, default: Any = None) -> Any:
    if v is None or v == "":
        return default
    return json.loads(v)


class Tx:
    """A connection bound to an open transaction."""

    def __init__(self, conn: Any, dialect: str):
        self._conn = conn
        self.dialect = dialect

    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self._conn.execute(self._sql(sql), tuple(params))
        return cur.rowcount if cur.rowcount is not None else 0

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._conn.execute(self._sql(sql), tuple(params))
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.fetchone(sql, params)
        return None if row is None else next(iter(row.values()))


class Database:
    def __init__(self, url: str, pool_size: int = 5):
        self.url = url
        if url.startswith("sqlite"):
            self.dialect = "sqlite"
            # sqlite://:memory:  |  sqlite:///relative.db  |  sqlite:////absolute/path.db
            rest = url.split("://", 1)[1] if "://" in url else ""
            if rest in ("", ":memory:", "/:memory:"):
                path = ":memory:"
            else:
                path = rest[1:] if rest.startswith("/") else rest
            self._sqlite = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._sqlite.execute("PRAGMA foreign_keys=ON")
            if path != ":memory:":
                self._sqlite.execute("PRAGMA journal_mode=WAL")
            self._lock = threading.RLock()
        elif url.startswith(("postgres://", "postgresql://")):
            self.dialect = "postgres"
            import psycopg  # noqa: F401  (imported lazily so SQLite-only installs work)

            self._pool: queue.Queue = queue.Queue(maxsize=pool_size)
            self._pool_size = pool_size
            self._created = 0
            self._pool_lock = threading.Lock()
        else:
            raise ValueError(f"unsupported database url {url!r}")

    # ------------------------------------------------------------------ connections

    def _pg_conn(self):
        import psycopg

        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            with self._pool_lock:
                if self._created < self._pool_size:
                    self._created += 1
                    return psycopg.connect(self.url, autocommit=False)
            conn = self._pool.get(timeout=30)
        if conn.closed:
            with self._pool_lock:
                self._created -= 1
            return self._pg_conn()
        return conn

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        if self.dialect == "sqlite":
            with self._lock:
                self._sqlite.execute("BEGIN IMMEDIATE")
                try:
                    yield Tx(self._sqlite, "sqlite")
                except BaseException:
                    self._sqlite.execute("ROLLBACK")
                    raise
                else:
                    self._sqlite.execute("COMMIT")
        else:
            conn = self._pg_conn()
            try:
                yield Tx(conn, "postgres")
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._pool.put(conn)

    # ----------------------------------------------------------- convenience (sync)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as tx:
            return tx.execute(sql, params)

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.transaction() as tx:
            return tx.fetchall(sql, params)

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.transaction() as tx:
            return tx.fetchone(sql, params)

    # ----------------------------------------------------------------------- async

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run `fn(tx, *args, **kwargs)` in a worker thread inside one transaction."""

        def _run():
            with self.transaction() as tx:
                return fn(tx, *args, **kwargs)

        return await anyio.to_thread.run_sync(_run)

    # ------------------------------------------------------------------- migrations

    @contextmanager
    def startup_lock(self, name: str) -> Iterator[None]:
        """Serialize startup work (migrations, seeding) across instances/services.
        Postgres: session advisory lock on a dedicated connection. SQLite: process lock."""
        if self.dialect == "sqlite":
            with self._lock:
                yield
            return
        import zlib

        conn = self._pg_conn()
        key = zlib.crc32(name.encode())
        try:
            conn.execute("SELECT pg_advisory_lock(%s)", (key,))
            conn.commit()
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
                conn.commit()
            finally:
                self._pool.put(conn)

    def migrate(self, directory: str | os.PathLike) -> list[str]:
        applied: list[str] = []
        with self.transaction() as tx:
            tx.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            done = {r["version"] for r in tx.fetchall("SELECT version FROM schema_migrations")}
        for f in sorted(Path(directory).glob("*.sql")):
            name = f.name
            if name.endswith(".pg.sql") and self.dialect != "postgres":
                continue
            if name.endswith(".sqlite.sql") and self.dialect != "sqlite":
                continue
            if name in done:
                continue
            sql = f.read_text()
            if self.dialect == "sqlite":
                with self._lock:
                    self._sqlite.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                    self._sqlite.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (name, now_iso())
                    )
            else:
                conn = self._pg_conn()
                try:
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)", (name, now_iso())
                    )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    self._pool.put(conn)
            applied.append(name)
        return applied

    def close(self) -> None:
        if self.dialect == "sqlite":
            self._sqlite.close()
        else:
            while True:
                try:
                    self._pool.get_nowait().close()
                except queue.Empty:
                    break


def expand_url(url: str) -> str:
    """Substitute {password} with DB_PASSWORD (injected from Secret Manager on Cloud Run)."""
    pw = os.environ.get("DB_PASSWORD")
    if pw and "{password}" in url:
        from urllib.parse import quote

        url = url.replace("{password}", quote(pw, safe=""))
    return url


def database_from_env(var: str = "DATABASE_URL", default: str = "sqlite://:memory:") -> Database:
    return Database(expand_url(os.environ.get(var, default)))
