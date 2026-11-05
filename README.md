# Enterprise Agent Platform (GCP)

Two separately deployed services:

| Service | Role | Talks to |
|---|---|---|
| **Governor** (`services/governor`) | Control plane + web console. Authoring and governing agent definitions (context, guardrails, knowledge, model, pattern, eval rules, output format). Draft → validate → eval → approve → publish → alias / canary / rollback. Catalog, policies, audit, dependency graph. | Its own Postgres DB; the runtime only for evals (`/internal/v1/execute`); publishes events to Pub/Sub. |
| **Agent Runtime** (`services/runtime`) | Data plane. Executes published, immutable agent versions. | Pulls definitions + effective policy from the governor **API** (`/internal/v1/resolve`), never its tables. Its own Postgres DB (runs, budgets, pgvector knowledge). |

`packages/eap_core` holds the shared contracts: the definition schema, the policy model, the signed call context, plugin contracts, the invoke contract, portable SQL, auth, events and tasks.

```
 console (IAP) ──HTTPS LB──▶ Governor ──Pub/Sub event (alias.moved …)──▶ Runtime /internal/v1/events
                                ▲    └──/internal/v1/execute (evals)──▶ Runtime
                                └───────── /internal/v1/resolve/{agent}/{alias}  (TTL + ETag) ◀── Runtime
 callers / MCP clients ─────────────────────────────────────────────▶ Runtime /v1/agents/{name}@{alias}/invoke, /mcp
```

## Key behaviours

- **Behaviour changes without a redeploy.** The runtime caches resolve bundles for a short TTL, revalidates them with `If-None-Match`, and drops entries when Pub/Sub events arrive (`alias.moved`, `version.*`, `policy.updated`, `model.status_changed`). If the governor is unreachable it serves the last bundle for up to `max_stale`.
- **Agents call agents.** A child call inherits the caller's identity (`on_behalf_of`) and trace ID. Its budget comes out of the caller's remaining budget, and its depth increases by one (platform cap 3). An agent may not appear twice in a call chain. Across processes the context travels as an HS256 JWT (`X-EAP-Call-Context`) that only platform services may forward.
- **Tighten-only policy.** BU overlays are validated (attempts to loosen are rejected) and merged with min / intersection / union, so even an unvalidated overlay can't loosen the platform policy. Hard controls are enforced in code: limits, model/tool allow-lists, required guardrails, release rules, budgets. Soft controls are policy text appended to the system prompt.
- **Release gate.** Evals and approvals are bound to the draft's content hash, so any edit invalidates them. Separation of duties is enforced: editors and the publisher can't approve their own change. A breaking contract change forces a major version. Agent-call cycles are rejected when you validate. If a model's status changes to deprecated, retired or blocked, every published version that uses it is flagged.
- **Patterns:** `single_shot`, `rag` (pgvector, cited sources), `research_loop` (tools + agents), `map_reduce` (fan-out to a child agent or a prompt), `human_in_the_loop` (`needs_input` → `POST /v1/runs/{id}/resume`).
- **Guardrails by stage:** `input`, `retrieval`, `tool_call`, `tool_result`, `output`. Types: PII (checksum-validated), prompt injection, topic deny-list, regex, max length, JSON schema, citations, tool allow-list. Actions: block, redact, warn.
- **Models:** Vertex Gemini (`generateContent`), Claude on Vertex (`rawPredict`), and a deterministic mock for dev/CI. Catalog fallbacks are supported, and `retired`/`blocked` models are refused.
- **Tools:** builtin, REST (forwards `X-On-Behalf-Of` and an ID token), MCP over streamable HTTP, and BigQuery (read-only, parameterised SQL).
- **Budgets:** per-run caps, plus BU daily budgets held as atomic Postgres counters (reserve before each model call, settle after).
- **Invoke modes:** sync, async (Cloud Tasks) and SSE stream. A minimal **MCP server** at `/mcp` exposes agents marked `exposure.mcp`.

## Quick start (local)

```bash
pip install -e packages/eap_core[postgres] -e services/governor -e services/runtime pytest pytest-asyncio
```

```bash
python -m pytest -q
```

Run the Postgres DDL and portability tests too. They need a server with pgvector where your user can `CREATE DATABASE`:

```bash
EAP_TEST_PG_DSN=postgresql://eap:eap@localhost:5432/postgres python -m pytest -q tests/test_postgres.py
```

Full local stack (Postgres + pgvector, console on :8080, runtime on :8081):

```bash
docker compose up --build
```

```bash
python examples/seed.py --governor http://localhost:8080 --runtime http://localhost:8081
```

Open http://localhost:8080 and, in the top bar, set a dev user (e.g. `admin@example.com` with roles `*:platform_admin`). Then invoke an agent:

```bash
curl -s localhost:8081/v1/agents/customer-profile@prod/invoke -H 'Content-Type: application/json' \
  -H 'X-Dev-User: dave@example.com' -H 'X-Dev-Roles: retail:viewer' -d '{"input": {"customer_id": "C-1001"}}'
```

Seeded examples (`examples/agents`):

- **customer-profile**: an orchestrator using `research_loop` that calls the CRM tool and the entity-resolver agent
- **entity-resolver**: `rag` over the `customer-master` pgvector source, with citations required
- **evaluator**: an LLM-as-judge using `single_shot`

## Repository layout

```
packages/eap_core/        schema, policy, context, plugins, contracts, db, auth, events, tasks, gcp
services/governor/        Starlette app, store/registry, release gate, evals, catalog, security, console (static SPA)
services/runtime/         Starlette app, resolver, executor + patterns, guardrails, models, tools, MCP, knowledge, budgets
infra/terraform/          one GCP project per environment (europe-west4): VPC/PSA, Cloud SQL PG16, Cloud Run x2,
                          Pub/Sub, Cloud Tasks, KMS/CMEK, Secret Manager, Artifact Registry, HTTPS LB + IAP, monitoring
cloudbuild.yaml           test (incl. Postgres) -> build -> push -> deploy
docker-compose.yml        local stack
examples/                 example agents, eval datasets (JSON / JSONL / CSV), knowledge, seed script
tests/                    unit + in-process e2e (both apps wired over ASGI) + MCP + Postgres
docs/                     model / protocol verification notes
```

## Auth modes

- **dev**: `X-Dev-User`, `X-Dev-Roles` ("bu:role,…") and `X-Dev-Kind` headers. Local use only.
- **iap**: console users. Their roles come from the governor's `role_bindings` table, and `EAP_BOOTSTRAP_ADMINS` seeds the first platform admin.
- **google_id**: service-to-service calls, Pub/Sub push and Cloud Tasks. `EAP_SERVICE_ACCOUNTS` maps service-account emails to logical names.
- **oidc**: end users calling the runtime. Roles come from the `eap_roles` claim (configurable).

Governor roles are granted per BU: `viewer`, `author`, `approver`, `publisher` and `bu_admin`. `platform_admin` is granted with `bu='*'`.

## Status and open items

See `CLAUDE.md` → *Status*. In particular, verify the Vertex model IDs and their regional availability (`docs/models.md`) before setting catalog models to `active`.
