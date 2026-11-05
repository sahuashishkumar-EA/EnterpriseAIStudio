# Agent Studio: technical and functional experts on one agent

## The idea

An agent has **one technical layer** and **one or more functional contexts**.

| Layer | Owner | Screen | Contains |
|---|---|---|---|
| Technical | technical expert (`author`) | 🛠️ Technical setup | system-level context (system prompt, instructions, static context), model + fallbacks, orchestration pattern, tools / knowledge / agent bindings, technical guardrails, limits, output format, contract, channels (API, MCP, chat app) |
| Functional | functional expert (`functional_author`) | 🧑‍💼 Functional studio | per context: role, goal, audience, behaviour (tone, length, format, language, emoji), always / never rules, facts & FAQ notes, web pages to learn from, boundaries (stick-to topics, refused topics + reply, hand-over triggers + reply, personal-data handling, disclaimer), examples, conversation starters |
| Platform | policy | Policies | soft controls appended last, required guardrails, limits |

A context is one audience or use case, e.g. *Retail customers* and *Branch advisors* on the same
`bank-assistant`. The caller picks one per conversation (`"context": "retail"` in the invoke
request, or the dropdown in the chat app). Otherwise the default context is used.

At run time the runtime composes the prompt in this order:
**system-level context → functional context → platform rules**
(`packages/eap_core/eap_core/compose.py`). The console's *What the model sees* panel shows the
same composition.

## Working on the same agent at the same time

- The technical screen saves only technical fields (`PUT …/draft/technical`). The studio saves
  one context at a time (`PUT …/draft/contexts/{id}`), so neither overwrites the other.
- Every save carries the fingerprint of the section the person loaded. If someone else changed
  that section in the meantime, the save is refused with *"changed by someone else"* (HTTP 409),
  so nothing is silently lost.
- Every editor is recorded on the draft. Outside dev mode, editors can't approve their own
  change.

## Functional guardrails are friendly

Refused topics, hand-over triggers and *ask not to share personal data* are enforced in code
**before** the model is called. The user gets the configured reply (status `ok`), and the run
is flagged (`responded:topic_policy`, `escalated`, …) for operators. *Redact* masks emails,
phone numbers, IBANs and card numbers in both input and output. Stick-to topics, tone,
always/never and examples are soft controls in the prompt.

## Web pages

Add URLs and click **Scan now**. The runtime fetches up to *max pages* per URL (same site by
default), strips navigation and scripts, chunks the text, embeds it (`EAP_WEB_EMBEDDING_MODEL`,
default `text-embedding-005`, with a fallback to a local embedding if unavailable) and stores it
in pgvector as knowledge source `web-<agent>-<context>`. Answers in that context are grounded in
the pages and cite them as `[n]`.

Safety: http(s) only, public hosts only (localhost, private ranges and the metadata server are
blocked, including after redirects), size, page and time limits. On GCP the runtime needs
**Cloud NAT** to reach the internet (`enable_nat`, on by default).

Prototype limitation: draft and published versions share one web index per context, so a
re-scan affects the live agent immediately.

## Try before publishing

The studio's **Live preview** chats with the draft, including unsaved edits, through the
runtime (`POST …/preview`) with conversation history. Sources, flags, latency and cost are shown
under each answer.

## Dev mode: one person does everything

When `EAP_RELAXED_RELEASE=true` (Terraform sets it for `environment = "dev"`):

- the person who edited a draft may also approve and publish it (still fully audited);
- **⚡ Quick publish** on the Overview tab runs every eval suite (creating a `smoke` suite from
  the conversation starters if there is none), approves, publishes and points `prod`.

Outside dev, separation of duties is enforced as before.

## Chat app

`services/chat` is a separate Cloud Run service with its own IAP sign-in. It lists agents that are
published to `prod` with **Offer in chat app** enabled, together with their contexts and starters.
It calls the runtime with its own service account plus `X-EAP-On-Behalf-Of: <signed-in user>`.
The runtime only accepts that header from delegating services (`EAP_DELEGATING_SERVICES=chat`),
and only for chat-enabled agents. Runs are recorded on behalf of the end user.

## Gemini API with your own key (Google AI Studio)

Besides Vertex AI, the runtime can call the **Gemini Developer API**
(`generativelanguage.googleapis.com`) with an API key from Google AI Studio (provider
`google_ai`, catalog entries `google-ai-flash` / `google-ai-pro`). The key goes into Secret
Manager through Terraform (`TF_VAR_google_ai_api_key`), and only the runtime can read it. When
a key is set, scanned web pages are embedded with `gemini-embedding-001` through the same key.
Thought signatures returned with function calls are passed back unchanged, as newer Gemini
models require.

A consumer Google AI Pro/Ultra subscription covers the Gemini app, not API usage. API calls use
the key's free tier, or paid billing on its Cloud project. Data handling follows the Gemini API
terms (the free tier may be used to improve Google products) and there is no EU data-residency
guarantee. Use Vertex AI for that.

## Seeing the orchestration

Every answer in the studio's live preview and in the chat app has **How this answer was made**:
the run's steps in order, e.g. guardrails, retrieval (with hit counts), each model call (model,
tokens, cost, and whether it chose to call tools), each tool call with its arguments and result,
and any child agents. The `bank-assistant` example uses the `research_loop` pattern with a
`calculator` tool, so a question like *"monthly payment on 300000 EUR over 20 years at 3.7%?"*
shows the model calling the calculator, then answering.

## Using a real model

Catalog → model → **Test** sends a tiny request through the runtime. If it answers, set the
status to `active`, then pick the model in the agent's Technical setup. If the test fails because
the model isn't served in `europe-west4`, edit the catalog entry's `location` (e.g. `europe-west1`
or `global`, after you've accepted the data-residency implications) and test again.

## Roles

| Role | Can |
|---|---|
| `functional_author` | create agents, edit functional contexts, scan web pages, live preview |
| `author` | the above + technical setup, eval suites, evals |
| `approver` / `publisher` | approve / publish, move aliases |
| `bu_admin`, `platform_admin` | everything (platform admin: catalog, platform policy) |
