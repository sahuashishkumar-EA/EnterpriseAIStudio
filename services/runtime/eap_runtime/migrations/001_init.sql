-- Runtime schema (portable: SQLite locally, Postgres 16 on Cloud SQL).
-- The runtime never reads governor tables; it only stores runs, budgets and knowledge.

CREATE TABLE runs (
    id               TEXT PRIMARY KEY,
    agent            TEXT NOT NULL,
    alias            TEXT,
    version          TEXT NOT NULL,
    content_hash     TEXT,
    mode             TEXT NOT NULL DEFAULT 'sync',   -- sync | async | stream | execute | child
    status           TEXT NOT NULL,                  -- queued | running | ok | partial | failed | needs_input
    input            TEXT NOT NULL DEFAULT '{}',
    output           TEXT,
    error            TEXT,
    trace_id         TEXT NOT NULL,
    parent_run_id    TEXT,
    principal        TEXT NOT NULL,
    on_behalf_of     TEXT NOT NULL,
    bu               TEXT,
    depth            INTEGER NOT NULL DEFAULT 0,
    cost_usd         DOUBLE PRECISION NOT NULL DEFAULT 0,
    input_tokens     BIGINT NOT NULL DEFAULT 0,
    output_tokens    BIGINT NOT NULL DEFAULT 0,
    latency_ms       BIGINT NOT NULL DEFAULT 0,
    steps            TEXT NOT NULL DEFAULT '[]',
    sources          TEXT NOT NULL DEFAULT '[]',
    needs_input      TEXT,
    state            TEXT,                           -- pattern state for human_in_the_loop resume
    idempotency_key  TEXT UNIQUE,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX runs_trace ON runs (trace_id);
CREATE INDEX runs_agent ON runs (agent, created_at);

-- Atomic spend counters (e.g. bu:<name>:<yyyy-mm-dd>). Reserve before a model call,
-- settle after, so concurrent runs cannot overshoot the limit.
CREATE TABLE budgets (
    key          TEXT PRIMARY KEY,
    limit_usd    DOUBLE PRECISION,
    spent_usd    DOUBLE PRECISION NOT NULL DEFAULT 0,
    reserved_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE knowledge_chunks (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,          -- catalog knowledge id
    doc_id      TEXT NOT NULL,
    chunk_no    INTEGER NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    uri         TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    embedding   TEXT NOT NULL,          -- JSON array (portable); pgvector column added on Postgres
    created_at  TEXT NOT NULL
);
CREATE INDEX knowledge_source ON knowledge_chunks (source_id, doc_id);
