-- Governor schema (portable: SQLite locally, Postgres 16 on Cloud SQL).
-- Timestamps are ISO-8601 TEXT, JSON documents are TEXT.

CREATE TABLE agents (
    name         TEXT PRIMARY KEY,
    owner_bu     TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    archived     INTEGER NOT NULL DEFAULT 0
);

-- One working draft per agent. Editing a draft changes its content hash, which
-- invalidates evals and approvals bound to the previous hash.
CREATE TABLE drafts (
    agent_name    TEXT PRIMARY KEY REFERENCES agents(name),
    definition    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    editors       TEXT NOT NULL DEFAULT '[]'
);

-- Immutable published versions. Only status/flag columns may change after insert.
CREATE TABLE versions (
    agent_name    TEXT NOT NULL REFERENCES agents(name),
    version       TEXT NOT NULL,
    major         INTEGER NOT NULL,
    minor         INTEGER NOT NULL,
    patch         INTEGER NOT NULL,
    definition    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    changelog     TEXT NOT NULL DEFAULT '',
    breaking      TEXT NOT NULL DEFAULT '[]',
    published_by  TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    eval_run_ids  TEXT NOT NULL DEFAULT '[]',
    approvals     TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'active',   -- active | flagged | deprecated
    flag_reason   TEXT,
    PRIMARY KEY (agent_name, version)
);
CREATE INDEX versions_hash ON versions (agent_name, content_hash);

CREATE TABLE aliases (
    agent_name      TEXT NOT NULL REFERENCES agents(name),
    alias           TEXT NOT NULL,
    version         TEXT NOT NULL,
    canary_version  TEXT,
    canary_weight   INTEGER NOT NULL DEFAULT 0,       -- percent of traffic 0..100
    updated_by      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (agent_name, alias)
);

CREATE TABLE alias_history (
    id              TEXT PRIMARY KEY,
    agent_name      TEXT NOT NULL,
    alias           TEXT NOT NULL,
    version         TEXT NOT NULL,
    canary_version  TEXT,
    canary_weight   INTEGER NOT NULL DEFAULT 0,
    previous_version TEXT,
    actor           TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    at              TEXT NOT NULL
);
CREATE INDEX alias_history_lookup ON alias_history (agent_name, alias, at);

-- Edges from a published version to what it depends on.
CREATE TABLE dependencies (
    agent_name  TEXT NOT NULL,
    version     TEXT NOT NULL,
    dep_kind    TEXT NOT NULL,      -- agent | tool | model | knowledge
    dep_ref     TEXT NOT NULL,
    dep_alias   TEXT,
    PRIMARY KEY (agent_name, version, dep_kind, dep_ref)
);
CREATE INDEX dependencies_reverse ON dependencies (dep_kind, dep_ref);

CREATE TABLE eval_suites (
    id          TEXT PRIMARY KEY,
    agent_name  TEXT NOT NULL REFERENCES agents(name),
    name        TEXT NOT NULL,
    cases       TEXT NOT NULL,          -- JSON list of {id, input, expected, assertions}
    assertions  TEXT NOT NULL DEFAULT '[]',
    required    INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL DEFAULT 'json',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (agent_name, name)
);

CREATE TABLE eval_runs (
    id            TEXT PRIMARY KEY,
    agent_name    TEXT NOT NULL,
    suite_id      TEXT NOT NULL REFERENCES eval_suites(id),
    target        TEXT NOT NULL,        -- draft | version
    target_version TEXT,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL,        -- queued | running | completed | error
    passed        INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    pass_rate     DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
    results       TEXT NOT NULL DEFAULT '[]',
    error         TEXT,
    started_by    TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);
CREATE INDEX eval_runs_hash ON eval_runs (agent_name, content_hash);

CREATE TABLE approvals (
    id            TEXT PRIMARY KEY,
    agent_name    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    approver      TEXT NOT NULL,
    decision      TEXT NOT NULL,        -- approve | reject
    comment       TEXT NOT NULL DEFAULT '',
    at            TEXT NOT NULL,
    UNIQUE (agent_name, content_hash, approver)
);

CREATE TABLE catalog_models (
    id            TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,        -- vertex_gemini | vertex_claude | mock
    model_id      TEXT NOT NULL,
    location      TEXT NOT NULL DEFAULT 'europe-west4',
    status        TEXT NOT NULL DEFAULT 'active',  -- active | preview | deprecated | retired | blocked
    capabilities  TEXT NOT NULL DEFAULT '{}',
    pricing       TEXT NOT NULL DEFAULT '{}',      -- {input_per_mtok, output_per_mtok}
    params        TEXT NOT NULL DEFAULT '{}',
    notes         TEXT NOT NULL DEFAULT '',
    updated_by    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE catalog_tools (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,  -- builtin | rest | mcp | bigquery
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    config              TEXT NOT NULL DEFAULT '{}',
    input_schema        TEXT NOT NULL DEFAULT '{}',
    data_classification TEXT NOT NULL DEFAULT 'internal',
    owner_bu            TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    updated_by          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE catalog_knowledge (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,  -- pgvector
    title               TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    config              TEXT NOT NULL DEFAULT '{}',
    data_classification TEXT NOT NULL DEFAULT 'internal',
    owner_bu            TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    updated_by          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- scope = 'platform' or 'bu:<name>'
CREATE TABLE policies (
    scope       TEXT PRIMARY KEY,
    document    TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Per-BU role bindings. bu='*' is platform-wide.
CREATE TABLE role_bindings (
    principal   TEXT NOT NULL,
    bu          TEXT NOT NULL,
    role        TEXT NOT NULL,
    granted_by  TEXT NOT NULL,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (principal, bu, role)
);

CREATE TABLE audit_log (
    id        TEXT PRIMARY KEY,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL,
    details   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_target ON audit_log (target, at);
