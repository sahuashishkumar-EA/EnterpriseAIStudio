# ADR-004: Version status and change-request state are derived from append-only events

Status: accepted, 2026-11-19

## Context

Spec section 6 makes version, audit and approval tables append-only with UPDATE revoked, yet lists `status` on `artifact_version` and `state` on `change_request`.

## Decision

Rows are written once with their initial value (`draft`, `submitted`). Later status is a `version_status_event` row and later state is the latest `workflow_step.state`. Current values are read as the latest event.

## Consequences

No UPDATE is ever needed, so the database can refuse it for every role. Queries that need current status join on the latest event.
