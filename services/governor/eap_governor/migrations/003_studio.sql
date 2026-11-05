-- Functional studio: web knowledge scans per agent functional context.
CREATE TABLE web_scans (
    id           TEXT PRIMARY KEY,
    agent_name   TEXT NOT NULL,
    context_id   TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    status       TEXT NOT NULL,          -- completed | error
    pages        INTEGER NOT NULL DEFAULT 0,
    chunks       INTEGER NOT NULL DEFAULT 0,
    details      TEXT NOT NULL DEFAULT '{}',
    started_by   TEXT NOT NULL,
    started_at   TEXT NOT NULL
);
CREATE INDEX web_scans_ctx ON web_scans (agent_name, context_id, started_at);
