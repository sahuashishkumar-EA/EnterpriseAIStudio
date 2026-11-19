# ADR-005: TEXT JSON with a generated JSONB column

Status: accepted, 2026-11-19

## Context

`eap_core.db` uses portable SQL so unit tests run on SQLite; the spec asks for JSONB.

## Decision

Content is stored canonical-JSON TEXT (the hashed form, C03-F4). On PostgreSQL a `content_jsonb` column is `GENERATED ALWAYS AS (content::jsonb) STORED`, which also rejects invalid JSON and can be indexed.

## Consequences

The same DDL is emitted for both dialects from one definition (`db/migrations`).
