"""Postgres 16 DDL + portability check. Runs when EAP_TEST_PG_DSN points at a server where
the user may CREATE DATABASE (pgvector must be installed), e.g.

  EAP_TEST_PG_DSN=postgresql://eap:eap@localhost:5432/postgres pytest tests/test_postgres.py
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("EAP_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="EAP_TEST_PG_DSN not set")
ROOT = Path(__file__).parent.parent


@pytest.fixture
def pg_dbs():
    import psycopg

    names = [f"eap_gov_{uuid.uuid4().hex[:8]}", f"eap_rt_{uuid.uuid4().hex[:8]}"]
    with psycopg.connect(DSN, autocommit=True) as c:
        for n in names:
            c.execute(f'CREATE DATABASE "{n}"')
    base = DSN.rsplit("/", 1)[0]
    yield [f"{base}/{n}" for n in names]
    with psycopg.connect(DSN, autocommit=True) as c:
        for n in names:
            c.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')


def test_migrations_apply_on_postgres(pg_dbs):
    from eap_core.db import Database

    gov, rt = Database(pg_dbs[0]), Database(pg_dbs[1])
    assert gov.migrate(ROOT / "services/governor/eap_governor/migrations") == ["001_init.sql", "002_isolate_database.pg.sql", "003_studio.sql"]
    assert rt.migrate(ROOT / "services/runtime/eap_runtime/migrations") == ["001_init.sql", "002_pgvector.pg.sql", "003_isolate_database.pg.sql"]
    assert gov.migrate(ROOT / "services/governor/eap_governor/migrations") == []  # idempotent
    cols = {r["column_name"]: r["udt_name"] for r in rt.fetchall(
        "SELECT column_name, udt_name FROM information_schema.columns WHERE table_name = 'knowledge_chunks'")}
    assert cols["embedding_vec"] == "vector"
    tables = {r["table_name"] for r in gov.fetchall(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert {"agents", "drafts", "versions", "aliases", "alias_history", "dependencies", "eval_suites", "eval_runs",
            "approvals", "catalog_models", "catalog_tools", "catalog_knowledge", "policies", "role_bindings",
            "audit_log"} <= tables
    gov.close()
    rt.close()


def test_full_flow_on_postgres(pg_dbs):
    from examples.seed import seed
    from tests.harness import build

    p = build(gov_db_url=pg_dbs[0], rt_db_url=pg_dbs[1])

    async def go():
        async with p.gov_client() as g, p.rt_async_client() as r:
            await seed(g, r)

    asyncio.run(go())
    with p.rt_client() as c:
        d = c.post("/v1/agents/customer-profile@prod/invoke", json={"input": {"customer_id": "C-1001"}},
                   headers={"X-Dev-User": "dave@example.com", "X-Dev-Roles": "retail:viewer"}).json()
    assert d["status"] == "ok" and d["output"]["name"] == "Anna de Vries"
    assert d["sources"] and d["sources"][0]["id"]  # pgvector similarity search
    audit = p.gov_db.fetchall("SELECT action FROM audit_log WHERE target LIKE ?", ("agent/customer-profile%",))
    assert any(a["action"] == "version.publish" for a in audit)
    p.gov_db.close()
    p.rt_db.close()
