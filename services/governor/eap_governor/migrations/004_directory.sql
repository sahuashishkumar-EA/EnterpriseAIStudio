-- Business units and the user directory. BU membership and roles stay in role_bindings
-- (principal = user email); the "member" role is an end user of the BU's agents (chat), no console rights.
CREATE TABLE business_units (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',   -- active | archived
    settings     TEXT NOT NULL DEFAULT '{}',       -- e.g. chat welcome text
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE users (
    email         TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',  -- active | disabled (disabled = no roles anywhere)
    default_bu    TEXT,
    attributes    TEXT NOT NULL DEFAULT '{}',      -- e.g. {"language": "nl", "job_title": "..."}
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

-- existing BUs (agent owners and role bindings) become directory entries
INSERT INTO business_units (id, name, created_by, created_at)
SELECT DISTINCT owner_bu, owner_bu, 'migration', '2026-11-20T00:00:00+00:00' FROM agents
UNION
SELECT DISTINCT bu, bu, 'migration', '2026-11-20T00:00:00+00:00' FROM role_bindings WHERE bu <> '*';

INSERT INTO users (email, created_by, created_at)
SELECT DISTINCT principal, 'migration', '2026-11-20T00:00:00+00:00' FROM role_bindings WHERE principal LIKE '%@%';
