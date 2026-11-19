# Enterprise Agent Platform (GCP): build brief for Claude Code

Source spec: "Enterprise Agent Platform: Requirements, Rationale and Modular Design"
(https://claude.ai/artifact/JtWS3MLBPkAaYPRirAu5dH). The spec maps to AWS; this repo targets **GCP**.

## Hard requirements
- Two separately deployed services:
  - **Governor** (control plane + web console): authoring and governing context, guardrails,
    knowledge sources, LLM selection, orchestration pattern, test/validation rules (write or upload),
    and output format. Lifecycle: draft -> validate -> eval -> approve -> publish -> rollback.
  - **Agent Runtime** (data plane): executes agents. It never reads governor tables. It pulls
    published, immutable definitions and the effective policy from the Governor **via API**, and
    changes behavior without a redeploy (short TTL cache + Pub/Sub `alias.moved` invalidation).
- Agents call agents with inherited identity (on-behalf-of), budget, depth (cap ~3) and trace ID; cycles are rejected.
- BUs may tighten platform policy, never loosen it. Hard controls go in code, soft controls in prompts.
- Infrastructure as code: Terraform, one GCP project per environment, default region europe-west4.

## Status (2026-11-05)
Everything below is implemented. The original `eap_core` was not available, so it was rebuilt from this brief.

- `packages/eap_core`: schema v1 (+ breaking-change detection / semver), policy (tighten-only
  validate + merge, check_definition, effective limits/guardrails), signed call context,
  plugin contracts with param schemas, invoke contract, portable SQL + migrations, auth (dev / IAP /
  Google ID / OIDC), events (one topic, `event_type` attribute), tasks (Cloud Tasks / local), gcp REST helpers.
- `services/governor`: registry, drafts, immutable semver versions, aliases + canary + history + rollback,
  dependency graph, release gate (hash-bound evals + approvals, separation of duties, breaking => major,
  static cycle check), eval service (JSON/JSONL/CSV; all assertion types incl. llm_judge via runtime),
  catalog, policy APIs, role bindings, audit, `GET /internal/v1/resolve/{name}/{alias}` with ETag,
  model status change => dependent versions flagged + events. Console: vanilla-JS SPA, forms generated
  from plugin param schemas.
- `services/runtime`: invoke (sync / async via Cloud Tasks / SSE), resolver client (TTL, ETag, event
  invalidation, stale-if-error), 5 patterns, guardrail enforcer by stage, Vertex Gemini / Claude rawPredict /
  mock adapters with fallbacks, tools (builtin, REST, MCP streamable HTTP, BigQuery read-only),
  pgvector knowledge, atomic budget counters, runs table, `/internal/v1/execute`, `/internal/v1/events`,
  MCP server at `/mcp`.
- `infra/terraform` (passes `terraform validate` on google/google-beta 6.50; console via IAP on Cloud Run by default, optional HTTPS LB), `cloudbuild.yaml`, `docker-compose.yml`, Dockerfiles.
- `tests/`: 54 tests (unit, in-process e2e with both apps wired over ASGI, MCP, Postgres 16 + pgvector).
  Run the Postgres tests with `EAP_TEST_PG_DSN=...`.

- Agent Studio (2026-11-05): functional contexts per agent (`FunctionalContext` in schema; composed
  by `eap_core/compose.py`), technical vs functional section saves with per-section conflict checks,
  `functional_author` role, friendly functional guardrails (`respond` action: topic_policy, escalation,
  pii refuse), web scanning into pgvector (`runtime/webscan.py`, SSRF-guarded), live preview, chat
  history, dev-mode quick publish (`EAP_RELAXED_RELEASE`), model Test button, `services/chat` demo app
  with delegated identity (`X-EAP-On-Behalf-Of`, `EAP_DELEGATING_SERVICES`). See `docs/studio.md`.

## Configuration-first platform (spec v1.0, 2026-11-19)
Build input: `AGENT_PLATFORM_BUILD_SPEC.md` v1.0 (Ashish Kumar Sahu). Design: "Configuration-First
Agent Platform on GCP — Re-engineering Design" (Claude doc). Notes and runbook: `docs/config-platform.md`.
Deviations are ADRs in `docs/adr/` (001 hybrid stack: Python back end, React/TS console, Python+TS SDK).

- Phase 1 slice built: C01 (`schemas/v1`, `packages/eap_metamodel`), C04 (`db/migrations`, append-only
  by trigger + privileges), C03 (`services/registry-api`), C05/C11 (`services/workflow`), C06
  (`services/eval-runner`), C07 (`services/resolver`), C09 (`sdk/python`, `sdk/ts`), C10 (`services/compiler`,
  mock adapter), C12 (`templates/mcp-server`), policies (`policies/`), C08 started. Phase 1 acceptance
  tests 1-10 pass in `tests/e2e/test_phase1.py` (test 6 also against PostgreSQL 16).
- Not yet: React console (C02), OTel export wiring, real Agent Platform adapter, `terraform validate`
  of `infra/terraform/config_platform.tf` (behind `enable_config_platform`, default false), governor data migration.
- The governor and runtime keep working unchanged; the runtime becomes the reference pro-code agent
  reading config through C09.
- Requirement IDs (e.g. `C03-F2`) go in code comments, test names and commit messages.

## Open items
1. Verify the Vertex model IDs and regional availability for europe-west4, then flip catalog models from
   `preview` to `active` (see `docs/models.md`). Not possible from the build session (docs host blocked).
2. Re-verify MCP 2026-07-28 details against the official spec (secondary sources only). Evaluate A2A
   if external agent interop is needed.
3. Dev deployment is live (project enterprise-ai-studio-513810, 2026-11-05): images built with
   `cloudbuild.images.yaml`, Terraform applied, console reachable via IAP on Cloud Run. Lessons:
   Cloud Tasks is not offered in europe-west4 (queues use `tasks_location`, default europe-west1);
   projects without an organization need a custom OAuth client set on the Cloud Run IAP settings
   in the console (Terraform cannot set it) with redirect URI `...clientIds/<id>:handleRedirect`.
   Still untested on GCP: evals end-to-end, the full `cloudbuild.yaml` CI pipeline, the runtime API
   from outside the VPC, knowledge ingestion.
4. Knowledge ingestion trusts the caller-supplied `embedding_model`. Consider resolving it from the
   catalog entry via the governor.
5. Hardening backlog: OpenTelemetry tracing export, per-agent / per-user rate limits, GCS-backed
   upload of large eval datasets, console tests in a browser.

## Conventions
- The runtime and governor share only `eap_core` and HTTP/event contracts. Never import one service from the other.
- Configuration-plane services share `eap_metamodel` and `eap_store`; runtime-path services (resolver,
  config MCP, SDK) never import `eap_store`. JSON Schemas in `schemas/v1` are the source of truth; after a
  schema change run `python schemas/tools/gen_types.py` and ship a migration in `eap_metamodel/migrations.py`.
- Registry tables are append-only: never write UPDATE/DELETE against them (status and state are events, ADR-004).
- SQL is portable (`?` placeholders, TEXT JSON/timestamps). Dialect-only migrations use `.pg.sql` / `.sqlite.sql`.
- Add a pattern / guardrail / tool kind by registering a `PluginSpec` in `eap_core/plugins.py` (the console
  form comes from its schema) and implementing it in the runtime.
