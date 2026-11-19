# Configuration-first platform (spec v1.0) — Phase 1 build notes and runbook

Design: "Configuration-First Agent Platform on GCP — Re-engineering Design" (Claude doc).
Spec: `AGENT_PLATFORM_BUILD_SPEC.md` v1.0, 19 Nov 2026.

## Where things are

| Component | Code |
| --- | --- |
| C01 metamodel | `schemas/v1/*.schema.json`, `packages/eap_metamodel`, `schemas/tools/gen_types.py` |
| C03 Registry API | `services/registry-api/eap_registry` (OpenAPI at `/openapi.json`) |
| C04 registry DB | `db/migrations/001_registry.{pg,sqlite}.sql`, `packages/eap_store`, seed `db/seed/pilot.json` |
| C05 workflow, C11 provisioning | `services/workflow/eap_workflow` |
| C06 eval runner | `services/eval-runner/eap_evalrunner` |
| C07 resolver | `services/resolver/eap_resolver` |
| C08 config MCP (Phase 2, started) | `services/config-mcp` |
| C09 SDK | `sdk/python/eap_agent_config`, `sdk/ts` |
| C10 compiler + adapters | `services/compiler/eap_compiler` (`adapters/mock.py`, `adapters/agent_platform.py` skeleton) |
| C12 MCP framework | `templates/mcp-server` (`eap_mcp_kit`, example `server.py`, contract tests) |
| Policies | `policies/eap/registry.rego` (+ `_test.rego`) |
| Launcher / image | `services/launcher` (`EAP_SERVICE=registry|workflow|resolver`) |
| GCP | `infra/terraform/config_platform.tf` (`enable_config_platform = true`) |
| Acceptance tests | `tests/e2e/test_phase1.py` (spec 10.1, items 1–10), `tests/test_config_platform.py` |

## Run

```bash
python -m pytest -q tests/e2e tests/test_config_platform.py templates/mcp-server/tests
```

```bash
cd sdk/ts && npm test
```

```bash
docker compose --profile config up --build
```

Then load the pilot: `POST /artifacts` for each entry of `db/seed/pilot.json` and submit one change
request, or call `eap_store.seed.seed()` + `PromotionEngine.bootstrap()` (dev only; refused once anything is active).

## Runbook: resolver (C07) — runtime path, on call

| Symptom | Check | Action |
| --- | --- | --- |
| `source: last-known-good` rising | `/healthz` stats; registry-internal health; Cloud SQL status | Agents keep running. Restore the registry; LKG is never evicted by events. |
| 503 `unavailable` | No LKG for that key (new agent × BU since the last restart) and registry down | Restore the registry; LKG files live on the instance and in Valkey. |
| 403 for an agent | `EAP_AGENT_IDENTITIES` / `eap_agent_id` claim; trusted-service list | Fix the identity mapping; never widen to `*`. |
| Change not visible | Pub/Sub subscription `*-resolver-invalidation` backlog / DLQ | Cache TTL (300 s) bounds staleness; replay the DLQ. |
| p95 > 50 ms | Valkey latency, instance count (min 2) | Scale instances; check Direct VPC egress. |

## Runbook: workflow (C05/C11)

* A change request stuck in `checking` or `provisioning`: re-enqueue `/internal/v1/tasks/checks` or
  `/internal/v1/tasks/provision` with the change request id; steps are idempotent.
* Provisioning failed: the platform was rolled back to the previous deployment (see `deployment`
  rows with `rolled_back`); hot configuration was not activated. Fix and submit a new change.
* Emergency: `POST /artifacts/{id}/rollback {"environment": "production", "emergency": true}`
  activates the last known good version now; the change request then waits for after-the-fact approval.

## Not done yet (Phase 1 backlog)

* React console (C02 basic): API and schemas are ready (`/schemas/{type}/latest`, field roles in `x-editors`).
* OpenTelemetry export wiring (attributes are produced: SDK `otel_attributes()`, C12 spans).
* Real Agent Platform adapter (ADR-002), Gen AI evaluation adapter (C06-F2).
* `terraform validate` of `config_platform.tf`; Cloud Build steps for `opa test` and `npm test`.
* Migration of existing governor agents into artifacts.
