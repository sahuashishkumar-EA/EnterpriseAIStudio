# ADR-001: Hybrid stack: Python back end, TypeScript console and SDK

Status: accepted, 2026-11-19

## Context

Spec section 3 defaults to TypeScript/Fastify for all back-end services. The repo already holds ~10k lines of tested Python (`eap_core`, release gate, resolver cache, evals, GCP helpers) and the team works in Python.

## Decision

Back-end services (C03, C05, C06, C07, C08, C10, C11, C13 ingest) are Python 3.12 + Starlette + Pydantic + jsonschema. The console (C02, C14, C15 UI) is React 19 + TypeScript + Vite. C09 ships as `sdk/python` and `sdk/ts` with identical names. JSON Schema 2020-12 in `schemas/v1` is the source of truth; `schemas/tools/gen_types.py` generates Python TypedDicts and TS interfaces, and CI runs it with `--check`.

## Consequences

OpenAPI is generated from the route table rather than from Zod schemas. Contract tests between console, Registry API, resolver and SDK must exercise both languages.
