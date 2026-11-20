# Business units: people, BU variants, chat sign-in, intelligent web

## 1. People and business units (Governor → People)

- **Business units** are directory entries: an id (used in policies and roles), a name, a description
  and a chat welcome text. Platform admins create them. A BU admin edits their own BU.
- **People** have a name, a default BU, a language and a status. A *disabled* person loses all access
  at once, in the console and in the chat app. Only platform admins can disable people.
- **Membership is per BU** and is expressed as roles. Any role in a BU makes the person a member of it.

| Role | What it allows |
|---|---|
| member | Use the BU's agents in the chat app (no console access) |
| viewer | See agents, tests and history in the console |
| functional_author | Shape behaviour in the functional studio. For a BU variant: also BU settings, tests and release |
| author | Technical setup of core agents |
| approver / publisher | Approve releases / put versions live |
| bu_admin | All of the above for the BU, plus manage its people |
| platform_admin | Everything, everywhere (granted on `*`) |

BU admins see and manage only their own BU's people and memberships. Every change is in the audit log
(`bu.create`, `bu.update`, `user.create`, `user.update`, `membership.set`).

API:

- `GET|POST /api/v1/business-units`
- `PUT /api/v1/business-units/{bu}`
- `GET /api/v1/users`
- `PUT /api/v1/users/{email}`
- `PUT /api/v1/users/{email}/memberships/{bu}` with `{"roles": [...]}`. An empty list removes the person from the BU.

## 2. One agent, different behaviour per BU: BU variants

**Responsibilities are split.** The technical owner (the core agent's BU, role `author`) owns:

- the core agent: system prompt, orchestration pattern, tools and technical guardrails
- *what business units may change*, under Technical setup → **Business-unit customization**:
  - which BUs may create a variant
  - which models they may choose (besides the core model)
  - whether they may:
    - add their own prompt rules
    - change the output format
    - define their own input/output contract
    - add knowledge and web scans
    - use live web search

A **BU's functional experts** (role `functional_author` in that BU) own their **variant**: Overview →
"+ Variant for my BU". A variant is an agent of its own (e.g. `bank-assistant-wealth`, owned by the
BU), with:

- **its own functional contexts**: audiences, tone, always/never, knowledge, boundaries, starters
- **BU settings** (the tab replaces Technical setup):
  - BU rules appended to the core prompt
  - model choice (from the offered list)
  - output format and citations
  - a BU-specific contract (if allowed)
  - stricter limits
  - chat / MCP exposure
- **its own tests, approvals and releases**: the BU's functional experts run them; the core team is
  not involved
- **a pinned core version**. A variant is built and tested on one published core version. When the
  core publishes a new version, live variants keep running on the version they were tested on. The
  variant shows "core x.y is live → Update to core x.y". That moves the draft to the new core (keeping
  everything the BU wrote), then the BU tests and releases again. The core's overview lists all
  variants and flags the ones on an older core.

The runtime does not know about variants. The governor composes core version + variant before
resolving, previewing and testing. Composition follows these rules:

- prompt = core prompt + BU rules
- model / contract / output = the BU's choice, if allowed
- limits = the stricter value of core and variant
- guardrails and knowledge = core plus the variant's additions
- data classification = the stricter one

The release gate validates the variant against the core's customization rules (`variant.*`
violations) and the BU's policy.

API:

- `POST /api/v1/agents/{core}/variants` with `{"bu"}`
- `PUT /api/v1/agents/{variant}/draft/bu` with `{"overlay", "exposure", "base"}`
- `POST /api/v1/agents/{variant}/rebase` with `{"version"?}`

## 3. Chat: sign in, choose a business unit, get the BU's edition

1. **Sign in.** On GCP this is IAP (Google account). Locally there is a simple email sign-in
   (signed cookie, dev only).
2. **Choose the BU** the person works for. The list comes from their memberships; the default BU is
   highlighted, the choice is remembered and can be switched. People without any BU see "ask your BU
   admin". Platform admins see every BU.
3. **The BU's agents** are listed:
   - its own agents and variants
   - agents shared with it (`exposure.invokers`: `authenticated` or `bu:<id>`)

   A BU's variant **replaces** the core agent in that BU's list (shown as the BU edition). The BU's
   welcome text is shown.
4. **Answers adapt to the person.** The chat app sends their name, BU, language and job title. The
   runtime adds them to the prompt ("About the person you are talking to"). Only delegating front
   ends may send this; newlines are stripped; it never grants permissions.

**Enforcement.** The chat app re-checks membership on every request. It calls the runtime with
`X-EAP-On-Behalf-Of` and `X-EAP-User-BU`. The runtime only runs chat-enabled agents that the BU may
use.

On GCP, IAP still decides who can reach the chat app (`chat_access_members`, e.g. `domain:yourbank.com`).
The directory then decides which BUs and agents each person gets. People must be members of a BU
to chat: add them under Governor → People.

## 4. Intelligent web

Per functional context, in the functional studio → What it knows:

- **This page**: the page and a few linked pages (max 25), as before.
- **Whole site, smart** crawl (max 200 pages):
  - reads `robots.txt` (and respects it) and the sitemap first
  - ranks every candidate link by relevance to the context (its name, audience, role, goal, topics,
    starters and note titles), using the URL and the link text
  - honours "only pages containing" / "skip pages containing" patterns
  - fetches a few pages at a time; tracking parameters are stripped
  - SSRF protection applies to every request
- **Keep live answers fresh**: re-scan daily, weekly or monthly. A daily Cloud Scheduler job
  (`scheduler.tf`, `web_refresh_schedule`) re-scans the web pages of **live** versions whose
  refresh period has passed. Which sites are read stays versioned; only their content is renewed
  (audited as `context.refresh`).
- **Search the web live**: the agent searches the web while answering, either *when needed* (the
  scanned pages and facts don't cover the question) or *for every question*. The search can cover
  the whole web, only listed sites, or never listed sites. The search uses Google Search grounding
  through Gemini:
  - with `GOOGLE_AI_API_KEY`: Gemini API
  - otherwise: Vertex AI
  - `EAP_WEB_SEARCH`: `auto|gemini|vertex|mock|off`
  - model: `EAP_WEB_SEARCH_MODEL`, default `gemini-2.5-flash`

  The agent's own model writes the answer from the results, with `[n]` citations to the pages found,
  under the agent's guardrails. Answers' "How this answer was made" shows the web search step.
  - **Governance:** not allowed for confidential/restricted agents (people's questions go to an
    external search service). For BU variants, only if the core allows it. `EAP_WEB_SEARCH=off`
    disables it platform-wide.
  - Grounded Gemini requests may be billed separately; check the current Gemini API pricing.

## Local demo

`scripts/run-local.sh --reset` seeds:

- the BUs Retail Banking and Wealth Management
- these people:
  - `ann@example.com`: member of both
  - `fred@example.com`: Wealth functional expert
  - `tom@example.com`: Retail technical
- the core `bank-assistant` (owned by retail, customizable) and its Wealth variant
  `bank-assistant-wealth`, with its own context, rules and model

Sign in to the chat app (http://localhost:8082) as `ann@example.com`. Retail shows `bank-assistant`;
Wealth shows the Wealth edition.
