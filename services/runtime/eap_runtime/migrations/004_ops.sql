-- Operations: which functional context answered, behaviour flags (escalated, responded:*),
-- end-user feedback and the review state used by the governor's Monitor inbox.
ALTER TABLE runs ADD COLUMN context_id TEXT;
ALTER TABLE runs ADD COLUMN flags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE runs ADD COLUMN feedback TEXT;
ALTER TABLE runs ADD COLUMN review_status TEXT;      -- NULL | open | resolved
ALTER TABLE runs ADD COLUMN assignee TEXT;
ALTER TABLE runs ADD COLUMN review_note TEXT;
ALTER TABLE runs ADD COLUMN reviewed_by TEXT;
ALTER TABLE runs ADD COLUMN reviewed_at TEXT;
CREATE INDEX runs_review ON runs (review_status, created_at);
CREATE INDEX runs_created ON runs (created_at);
