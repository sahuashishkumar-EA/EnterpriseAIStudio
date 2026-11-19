-- C04 Config registry database. Generated from one definition for both dialects; keep them in step.
-- SQLite twin for unit tests. Append-only is enforced with triggers.

-- One row per configuration artifact (C04-F1). Insert-only.
CREATE TABLE artifact (
    id          TEXT NOT NULL UNIQUE,
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('agent','role','contract','reasoning','identity_ref','skill','tool_def',
                                              'knowledge_source','model_policy','guardrail_policy','eval_suite','profile','binding')),
    name        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (type, name)
);

-- Immutable versions. Drafts are never edited: a new draft is a new row (C03-F3, principle 3).
-- `status` is the status at insert ('draft'); later status comes from version_status_event (ADR-004).
CREATE TABLE artifact_version (
    id                 TEXT NOT NULL UNIQUE,
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id        TEXT NOT NULL REFERENCES artifact(id),
    version            INTEGER NOT NULL CHECK (version >= 1),
    schema_version     TEXT NOT NULL,
    content            TEXT NOT NULL,
    content_hash       TEXT NOT NULL CHECK (length(content_hash) = 64),
    parent_version_id  TEXT REFERENCES artifact_version(id),
    author             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status = 'draft'),
    UNIQUE (artifact_id, version)
);

CREATE TABLE version_status_event (
    id          TEXT NOT NULL UNIQUE,
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  TEXT NOT NULL REFERENCES artifact_version(id),
    status      TEXT NOT NULL CHECK (status IN ('draft','in_review','approved','rejected','retired')),
    actor       TEXT NOT NULL,
    change_request_id TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX version_status_event_v ON version_status_event (version_id, seq);

-- Change requests: immutable header; state = latest workflow_step.state (C05, ADR-004).
CREATE TABLE change_request (
    id            TEXT NOT NULL UNIQUE,
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_ids   TEXT NOT NULL,          -- JSON array
    reason        TEXT NOT NULL,
    risk_tier     TEXT NOT NULL,
    environment   TEXT NOT NULL CHECK (environment IN ('dev','test','production')),
    requested_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'submitted'
);

CREATE TABLE workflow_step (
    id                 TEXT NOT NULL UNIQUE,
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id  TEXT NOT NULL REFERENCES change_request(id),
    step               TEXT NOT NULL,     -- schema | policy | evaluation | routing | approval | activation | provisioning | rollback
    result             TEXT NOT NULL,     -- passed | failed | pending | info
    state              TEXT NOT NULL,     -- change request state after this step
    detail             TEXT NOT NULL DEFAULT '{}',
    at                 TEXT NOT NULL
);
CREATE INDEX workflow_step_cr ON workflow_step (change_request_id, seq);

CREATE TABLE approval (
    id                 TEXT NOT NULL UNIQUE,
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id  TEXT NOT NULL REFERENCES change_request(id),
    approver           TEXT NOT NULL,
    role               TEXT NOT NULL,
    decision           TEXT NOT NULL CHECK (decision IN ('approve','reject')),
    comment            TEXT NOT NULL DEFAULT '',
    at                 TEXT NOT NULL,
    UNIQUE (change_request_id, approver, role)
);

CREATE TABLE evaluation_run (
    id                      TEXT NOT NULL UNIQUE,
    seq                     INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id              TEXT NOT NULL REFERENCES artifact_version(id),
    suite_version_id        TEXT,
    test_set_ref            TEXT,
    environment             TEXT NOT NULL DEFAULT 'dev',
    scores                  TEXT NOT NULL,
    passed                  INTEGER NOT NULL CHECK (passed IN (0, 1)),
    compared_to_version_id  TEXT,
    change_request_id       TEXT,
    at                      TEXT NOT NULL
);
CREATE INDEX evaluation_run_v ON evaluation_run (version_id, seq);

-- Current pointer = latest row per (artifact_id, environment) (C04-F3, C05-F5).
CREATE TABLE active_pointer (
    id                 TEXT NOT NULL UNIQUE,
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id        TEXT NOT NULL REFERENCES artifact(id),
    environment        TEXT NOT NULL CHECK (environment IN ('dev','test','production')),
    version_id         TEXT NOT NULL REFERENCES artifact_version(id),
    activated_by       TEXT NOT NULL,
    activated_at       TEXT NOT NULL,
    change_request_id  TEXT
);
CREATE INDEX active_pointer_current ON active_pointer (artifact_id, environment, seq);

CREATE TABLE deployment (
    id                  TEXT NOT NULL UNIQUE,
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_version_id    TEXT NOT NULL,
    profile_version_id  TEXT,
    channel             TEXT NOT NULL,
    platform            TEXT NOT NULL,
    environment         TEXT NOT NULL,
    adapter_version     TEXT NOT NULL,
    status              TEXT NOT NULL,     -- succeeded | failed | rolled_back
    detail              TEXT NOT NULL DEFAULT '{}',
    change_request_id   TEXT,
    at                  TEXT NOT NULL
);

CREATE TABLE audit_log (
    id          TEXT NOT NULL UNIQUE,
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    version_id  TEXT,
    at          TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_log_target ON audit_log (target, seq);

CREATE TABLE config_read_log (
    id              TEXT NOT NULL UNIQUE,
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_identity  TEXT NOT NULL,
    artifact_ids    TEXT NOT NULL,
    version_ids     TEXT NOT NULL,
    environment     TEXT NOT NULL,
    source          TEXT NOT NULL,
    at              TEXT NOT NULL
);

-- Named approvers per role and scope (C05-F3). Not part of the append-only set; revocation deletes.
CREATE TABLE role_binding (
    principal   TEXT NOT NULL,
    role        TEXT NOT NULL,
    scope       TEXT NOT NULL,            -- a business unit, an agent id, or '*'
    granted_by  TEXT NOT NULL,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (principal, role, scope)
);

CREATE TRIGGER artifact_no_update BEFORE UPDATE ON artifact BEGIN SELECT RAISE(ABORT, 'append-only table artifact'); END;
CREATE TRIGGER artifact_no_delete BEFORE DELETE ON artifact BEGIN SELECT RAISE(ABORT, 'append-only table artifact'); END;
CREATE TRIGGER artifact_version_no_update BEFORE UPDATE ON artifact_version BEGIN SELECT RAISE(ABORT, 'append-only table artifact_version'); END;
CREATE TRIGGER artifact_version_no_delete BEFORE DELETE ON artifact_version BEGIN SELECT RAISE(ABORT, 'append-only table artifact_version'); END;
CREATE TRIGGER version_status_event_no_update BEFORE UPDATE ON version_status_event BEGIN SELECT RAISE(ABORT, 'append-only table version_status_event'); END;
CREATE TRIGGER version_status_event_no_delete BEFORE DELETE ON version_status_event BEGIN SELECT RAISE(ABORT, 'append-only table version_status_event'); END;
CREATE TRIGGER change_request_no_update BEFORE UPDATE ON change_request BEGIN SELECT RAISE(ABORT, 'append-only table change_request'); END;
CREATE TRIGGER change_request_no_delete BEFORE DELETE ON change_request BEGIN SELECT RAISE(ABORT, 'append-only table change_request'); END;
CREATE TRIGGER workflow_step_no_update BEFORE UPDATE ON workflow_step BEGIN SELECT RAISE(ABORT, 'append-only table workflow_step'); END;
CREATE TRIGGER workflow_step_no_delete BEFORE DELETE ON workflow_step BEGIN SELECT RAISE(ABORT, 'append-only table workflow_step'); END;
CREATE TRIGGER approval_no_update BEFORE UPDATE ON approval BEGIN SELECT RAISE(ABORT, 'append-only table approval'); END;
CREATE TRIGGER approval_no_delete BEFORE DELETE ON approval BEGIN SELECT RAISE(ABORT, 'append-only table approval'); END;
CREATE TRIGGER evaluation_run_no_update BEFORE UPDATE ON evaluation_run BEGIN SELECT RAISE(ABORT, 'append-only table evaluation_run'); END;
CREATE TRIGGER evaluation_run_no_delete BEFORE DELETE ON evaluation_run BEGIN SELECT RAISE(ABORT, 'append-only table evaluation_run'); END;
CREATE TRIGGER active_pointer_no_update BEFORE UPDATE ON active_pointer BEGIN SELECT RAISE(ABORT, 'append-only table active_pointer'); END;
CREATE TRIGGER active_pointer_no_delete BEFORE DELETE ON active_pointer BEGIN SELECT RAISE(ABORT, 'append-only table active_pointer'); END;
CREATE TRIGGER deployment_no_update BEFORE UPDATE ON deployment BEGIN SELECT RAISE(ABORT, 'append-only table deployment'); END;
CREATE TRIGGER deployment_no_delete BEFORE DELETE ON deployment BEGIN SELECT RAISE(ABORT, 'append-only table deployment'); END;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'append-only table audit_log'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'append-only table audit_log'); END;
CREATE TRIGGER config_read_log_no_update BEFORE UPDATE ON config_read_log BEGIN SELECT RAISE(ABORT, 'append-only table config_read_log'); END;
CREATE TRIGGER config_read_log_no_delete BEFORE DELETE ON config_read_log BEGIN SELECT RAISE(ABORT, 'append-only table config_read_log'); END;
