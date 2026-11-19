-- C04 Config registry database. Generated from one definition for both dialects; keep them in step.
-- Cloud SQL for PostgreSQL 16. Objects live in schema `registry`; connect with
-- options=-csearch_path=registry,public. Append-only is enforced twice (C04 non-functional):
--   1. registry_app (the role every service login is granted) has SELECT/INSERT only;
--   2. triggers reject UPDATE/DELETE/TRUNCATE for every role, administrators included,
--      unless they own the table and disable the trigger (owner = migration role only).
CREATE SCHEMA IF NOT EXISTS registry;
SET search_path TO registry, public;

-- One row per configuration artifact (C04-F1). Insert-only.
CREATE TABLE artifact (
    id          TEXT PRIMARY KEY,
    seq         BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
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
    id                 TEXT PRIMARY KEY,
    seq                BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    artifact_id        TEXT NOT NULL REFERENCES artifact(id),
    version            INTEGER NOT NULL CHECK (version >= 1),
    schema_version     TEXT NOT NULL,
    content            TEXT NOT NULL,
    content_jsonb    JSONB GENERATED ALWAYS AS (content::jsonb) STORED,
    content_hash       TEXT NOT NULL CHECK (length(content_hash) = 64),
    parent_version_id  TEXT REFERENCES artifact_version(id),
    author             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status = 'draft'),
    UNIQUE (artifact_id, version)
);

CREATE TABLE version_status_event (
    id          TEXT PRIMARY KEY,
    seq         BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    version_id  TEXT NOT NULL REFERENCES artifact_version(id),
    status      TEXT NOT NULL CHECK (status IN ('draft','in_review','approved','rejected','retired')),
    actor       TEXT NOT NULL,
    change_request_id TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX version_status_event_v ON version_status_event (version_id, seq);

-- Change requests: immutable header; state = latest workflow_step.state (C05, ADR-004).
CREATE TABLE change_request (
    id            TEXT PRIMARY KEY,
    seq           BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    version_ids   TEXT NOT NULL,          -- JSON array
    reason        TEXT NOT NULL,
    risk_tier     TEXT NOT NULL,
    environment   TEXT NOT NULL CHECK (environment IN ('dev','test','production')),
    requested_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'submitted'
);

CREATE TABLE workflow_step (
    id                 TEXT PRIMARY KEY,
    seq                BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    change_request_id  TEXT NOT NULL REFERENCES change_request(id),
    step               TEXT NOT NULL,     -- schema | policy | evaluation | routing | approval | activation | provisioning | rollback
    result             TEXT NOT NULL,     -- passed | failed | pending | info
    state              TEXT NOT NULL,     -- change request state after this step
    detail             TEXT NOT NULL DEFAULT '{}',
    detail_jsonb    JSONB GENERATED ALWAYS AS (detail::jsonb) STORED,
    at                 TEXT NOT NULL
);
CREATE INDEX workflow_step_cr ON workflow_step (change_request_id, seq);

CREATE TABLE approval (
    id                 TEXT PRIMARY KEY,
    seq                BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    change_request_id  TEXT NOT NULL REFERENCES change_request(id),
    approver           TEXT NOT NULL,
    role               TEXT NOT NULL,
    decision           TEXT NOT NULL CHECK (decision IN ('approve','reject')),
    comment            TEXT NOT NULL DEFAULT '',
    at                 TEXT NOT NULL,
    UNIQUE (change_request_id, approver, role)
);

CREATE TABLE evaluation_run (
    id                      TEXT PRIMARY KEY,
    seq                     BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    version_id              TEXT NOT NULL REFERENCES artifact_version(id),
    suite_version_id        TEXT,
    test_set_ref            TEXT,
    environment             TEXT NOT NULL DEFAULT 'dev',
    scores                  TEXT NOT NULL,
    scores_jsonb    JSONB GENERATED ALWAYS AS (scores::jsonb) STORED,
    passed                  INTEGER NOT NULL CHECK (passed IN (0, 1)),
    compared_to_version_id  TEXT,
    change_request_id       TEXT,
    at                      TEXT NOT NULL
);
CREATE INDEX evaluation_run_v ON evaluation_run (version_id, seq);

-- Current pointer = latest row per (artifact_id, environment) (C04-F3, C05-F5).
CREATE TABLE active_pointer (
    id                 TEXT PRIMARY KEY,
    seq                BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    artifact_id        TEXT NOT NULL REFERENCES artifact(id),
    environment        TEXT NOT NULL CHECK (environment IN ('dev','test','production')),
    version_id         TEXT NOT NULL REFERENCES artifact_version(id),
    activated_by       TEXT NOT NULL,
    activated_at       TEXT NOT NULL,
    change_request_id  TEXT
);
CREATE INDEX active_pointer_current ON active_pointer (artifact_id, environment, seq);

CREATE TABLE deployment (
    id                  TEXT PRIMARY KEY,
    seq                 BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
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
    id          TEXT PRIMARY KEY,
    seq         BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    version_id  TEXT,
    at          TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_log_target ON audit_log (target, seq);

CREATE TABLE config_read_log (
    id              TEXT PRIMARY KEY,
    seq             BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
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

CREATE OR REPLACE FUNCTION registry.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % is not allowed', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER artifact_append_only BEFORE UPDATE OR DELETE ON registry.artifact FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER artifact_no_truncate BEFORE TRUNCATE ON registry.artifact FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER artifact_version_append_only BEFORE UPDATE OR DELETE ON registry.artifact_version FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER artifact_version_no_truncate BEFORE TRUNCATE ON registry.artifact_version FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER version_status_event_append_only BEFORE UPDATE OR DELETE ON registry.version_status_event FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER version_status_event_no_truncate BEFORE TRUNCATE ON registry.version_status_event FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER change_request_append_only BEFORE UPDATE OR DELETE ON registry.change_request FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER change_request_no_truncate BEFORE TRUNCATE ON registry.change_request FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER workflow_step_append_only BEFORE UPDATE OR DELETE ON registry.workflow_step FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER workflow_step_no_truncate BEFORE TRUNCATE ON registry.workflow_step FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER approval_append_only BEFORE UPDATE OR DELETE ON registry.approval FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER approval_no_truncate BEFORE TRUNCATE ON registry.approval FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER evaluation_run_append_only BEFORE UPDATE OR DELETE ON registry.evaluation_run FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER evaluation_run_no_truncate BEFORE TRUNCATE ON registry.evaluation_run FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER active_pointer_append_only BEFORE UPDATE OR DELETE ON registry.active_pointer FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER active_pointer_no_truncate BEFORE TRUNCATE ON registry.active_pointer FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER deployment_append_only BEFORE UPDATE OR DELETE ON registry.deployment FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER deployment_no_truncate BEFORE TRUNCATE ON registry.deployment FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON registry.audit_log FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON registry.audit_log FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER config_read_log_append_only BEFORE UPDATE OR DELETE ON registry.config_read_log FOR EACH ROW EXECUTE FUNCTION registry.forbid_mutation();
CREATE TRIGGER config_read_log_no_truncate BEFORE TRUNCATE ON registry.config_read_log FOR EACH STATEMENT EXECUTE FUNCTION registry.forbid_mutation();

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'registry_app') THEN
    CREATE ROLE registry_app NOLOGIN;
  END IF;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA registry FROM PUBLIC;
GRANT USAGE ON SCHEMA registry TO registry_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA registry TO registry_app;
REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA registry FROM registry_app;
-- role_binding is the one mutable table (grants are revoked by deleting the row).
GRANT DELETE ON registry.role_binding TO registry_app;
