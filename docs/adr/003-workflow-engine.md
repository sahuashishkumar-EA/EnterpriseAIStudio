# ADR-003: Workflow on the database plus Cloud Tasks

Status: accepted, 2026-11-19

## Context

C05 and C11 need durable, retryable steps.

## Decision

Keep the spec default: a state machine over append-only `workflow_step` rows. Cloud Tasks (already provisioned) delivers step execution and C11 jobs with retries; every step is idempotent. Locally steps run inline.

## Consequences

Revisit Temporal or Cloud Workflows if human steps or long timers outgrow this.
