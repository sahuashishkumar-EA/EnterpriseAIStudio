# Model and protocol verification notes

Written 2026-11-05. Re-verify before production. Model availability on Vertex AI
changes by region and over time.

## Vertex AI models in the seeded catalog

All real models are seeded with status **`preview`**. A published agent can't use a
preview model (`check_definition` reports `model_inactive`). A platform admin sets a model
to `active` (`PATCH /api/v1/catalog/models/{id}/status`) only after checking these three things:

1. The model ID exists in Model Garden for the project.
2. The model is served from the configured `location` (default `europe-west4`). Alternatively,
   set `location` to an `eu` multi-region / `global` endpoint and record the data-residency decision.
3. The pricing in the catalog entry matches Vertex AI pricing. The seeded prices are placeholders.

| Catalog id | Provider | Model id | What we know |
|---|---|---|---|
| gemini-2.5-flash-ew4 / gemini-2.5-pro-ew4 | vertex_gemini | gemini-2.5-flash / gemini-2.5-pro | Listed in the Vertex Gemini model docs. Per-region availability for europe-west4 could not be confirmed from this environment (docs.cloud.google.com is not reachable from the build session). Gemini 3 preview models are reported to be **global-endpoint only**, so they were not seeded. |
| claude-{opus,sonnet,haiku}-5-5-eu | vertex_claude | claude-opus-5-5 / claude-sonnet-5-5 / claude-haiku-5-5 | Current Claude models use the bare model id on Vertex (no `@date` suffix). Google's data-residency page lists europe-west4 for some Claude 4.x models, but other pages list only europe-west1 or offer the models via `global` / `eu` endpoints. Confirm each model for europe-west4. If a model is only offered via the `eu` multi-region, set the entry's `params.endpoint` to the correct host. |

The Claude adapter sends the Anthropic Messages body to `:rawPredict` with
`anthropic_version` (default `vertex-2023-10-16`, overridable per catalog entry via
`params.anthropic_version`). It never sends sampling parameters (`temperature`/`top_p`/`top_k`),
because newer Claude models reject them, and it leaves thinking at the model default. If a
definition sets `params.effort`, the adapter sends it as `output_config.effort`. A
`stop_reason: "refusal"` maps to run status `failed` with code `model_refused`. The Gemini
adapter uses `generateContent`. It sets `responseMimeType: application/json` only when no
tools are offered.

Embeddings for pgvector use 768 dimensions (`text-embedding-005`, or
`gemini-embedding-001` with `outputDimensionality=768`) via `:predict` in `EAP_EMBEDDING_LOCATION`.

## MCP

The current MCP protocol revision is reported as **2026-07-28**. It is stateless: there is no
initialize handshake, and the version is sent per request in the `MCP-Protocol-Version` header
and in `params._meta["io.modelcontextprotocol/protocolVersion"]`. This could only be checked
through secondary sources, because modelcontextprotocol.io is not reachable from the build
session. Accordingly:

- The **server** (`/mcp`) accepts both the stateless flow and the classic `initialize` flow
  (2025-11-25, 2025-06-18, 2025-03-26), never issues `Mcp-Session-Id`, rejects batches,
  validates `Origin`, and answers `GET` with 405.
- The **client** tries the stateless 2026-07-28 flow first. On a protocol-level rejection it
  falls back to `initialize` with 2025-11-25 and sends `Mcp-Session-Id` on later requests.
  It handles JSON and SSE responses.

Re-check against the official specification, and against A2A if agent-to-agent interop
with external platforms is needed, before relying on newer features.
