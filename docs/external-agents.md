# Agents built on other platforms (Copilot Studio, any HTTP agent)

An agent can be built and run outside this platform, for example in **Microsoft Copilot Studio**, and
still be governed by the Governor. The Governor keeps ownership of:

| What | How |
|---|---|
| Behaviour (instructions, functional contexts, platform rules) | Authored in the console as usual. Going live publishes a new **profile** that the external agent reads. |
| Hard controls (topic policy, hand-over, personal data, blocked terms, …) | Enforced in code by the **check API** around every turn, not by the external prompt. |
| Release gate | Tests in Release → ① Test run against the **real external agent** (Direct Line / HTTP), then review and go live as usual. |
| Change propagation | A **signed webhook** tells the external platform when a new version is live. Polling the profile with ETag also works. |
| Monitoring and audit | Every check and reported turn is a run (mode `external`): Monitor inbox, conversations and dashboard cover it. |

What stays on the external platform: the conversation runtime, its own model, connectors and
channels. The model, orchestration and tools in Technical setup are then used only for the live
preview in the console.

## 1. Mark the agent as external

In **Technical setup → Where this agent runs**, choose:

- **Microsoft Copilot Studio**: tests talk to the bot through Bot Framework **Direct Line 3.0**.
  *Secret name* is the name of a secret holding the Direct Line secret (Copilot Studio: Settings →
  Security → Web channel security).
- **Other platform (HTTP)**: tests `POST` each case to *Agent endpoint* as
  `{"message", "input", "context", "history"}` with `Authorization: Bearer <secret>`. The endpoint
  must return `{"output": …}` (or `reply` / `text`), optionally with `flags`.
- **A2A agent (preview)**: treated like HTTP for now. The A2A protocol itself is not implemented.

Also set:

- **Allowed callers**: who may use the governance API for this agent. Use the Entra ID app id
  (`azp`/`appid` claim) of the connector, a service name, or `user:<email>`.
- **Change webhook** (optional) and the **secret name** used to sign it.

Secrets are referenced **by name only**. On GCP list the names in `external_agent_secrets` (Terraform
creates empty Secret Manager secrets and mounts them on the governor as environment variables), then
add the values:

```bash
printf '%s' "<direct-line-secret>" | gcloud secrets versions add eap-dev-ext-directline-hr-copilot --data-file=-
```

Locally, export an environment variable with that name before `scripts/run-local.sh`.

## 2. Deploy the edge (public governance API)

The runtime and governor are internal. External platforms reach the governance API through the
**edge** service: the runtime image with `EAP_EDGE=true`. It serves only `/gov/v1/*`, `/mcp` and
`/healthz`, and authenticates OIDC tokens in the application.

```hcl
# infra/terraform/envs/dev.tfvars (or an *.auto.tfvars)
enable_edge            = true
edge_oidc_issuer       = "https://login.microsoftonline.com/<tenant-id>/v2.0"
edge_oidc_audience     = "api://<app-id-of-the-eap-api-registration>"
external_agent_secrets = ["DIRECTLINE_HR_COPILOT", "HR_COPILOT_WEBHOOK_SECRET"]
```

Entra ID setup (once per tenant):

1. **App registration "EAP governance API"**: Expose an API with Application ID URI `api://<app-id>`
   (this is `edge_oidc_audience`). In the manifest set `"accessTokenAcceptedVersion": 2` so tokens
   carry the v2 issuer above.
2. **App registration for the caller** (the Copilot Studio connector / flow): create a client secret
   and grant it an application permission (app role) on the EAP API. Its tokens have `idtyp=app` and
   are treated as service callers.
3. Put the caller's **application (client) id** in the agent's *Allowed callers*.

`terraform output edge_url` prints the URL. The governor shows it in the hosting section.

## 3. The governance API

All endpoints take `?alias=` (default `prod`). Errors use `{"error": {"code", "message"}}`.

### Profile: what the agent should be

```http
GET /gov/v1/agents/{name}/profile?context=retail
Authorization: Bearer <token>
If-None-Match: "<etag from last time>"
```

Returns `304` when unchanged, otherwise:

```json
{
  "agent": "hr-copilot", "version": "1.2.0", "platform": "copilot_studio",
  "context": {"id": "staff", "name": "Employees", "starters": ["…"], "language": "match the user"},
  "contexts": [{"id": "staff", "name": "Employees"}],
  "instructions": "# System context … # Functional context: Employees … # Platform rules …",
  "guardrails": [{"type": "topic_policy", "stage": "input", "action": "respond"}, …],
  "limits": {…}, "model": "gemini-flash", "knowledge": ["web-hr-copilot-staff--v1-2-0"],
  "check_api": {"input": "/gov/v1/agents/hr-copilot/check/input", …}
}
```

Use `instructions` as the external agent's instructions / system prompt.

### Check input: before the agent answers

```http
POST /gov/v1/agents/{name}/check/input
{"message": "I want to report harassment", "context": "staff", "user": "eve@contoso.com", "session_id": "conv-123"}
```

```json
{"check_id": "…", "decision": "respond", "message": "I'm connecting you with an HR advisor.",
 "text": "I want to report harassment", "flags": ["escalated"], "findings": […], "version": "1.2.0"}
```

The `decision` field tells the external agent what to do:

- `allow`: continue with `text`, which has personal data redacted when the context says so.
- `respond`: send `message` to the user and stop. A hand-over (`escalated`) appears in the Monitor inbox.
- `block`: send `message` and stop.

### Check output: before the answer is shown

```http
POST /gov/v1/agents/{name}/check/output
{"check_id": "<from check/input>", "output": "You have 12 days left.", "context": "staff"}
```

Returns `decision` (`allow` / `block`) and the `output` to show (redacted if needed). Passing
`check_id` completes the same run, with latency and the guardrail steps on its timeline.

### Report a finished turn (when checks cannot be called inline)

```http
POST /gov/v1/agents/{name}/runs
{"message": "…", "output": "…", "context": "staff", "user": "eve@contoso.com", "status": "ok",
 "latency_ms": 900, "cost_usd": 0.0004, "flags": [], "session_id": "conv-123"}
```

### Change webhook

When a version goes live (publish, rollback, canary change), the governor posts:

```json
{"event": "agent.version_live", "agent": "hr-copilot", "alias": "prod", "version": "1.3.0",
 "canary_version": null, "content_hash": "sha256:…", "profile_url": "https://…/gov/v1/agents/hr-copilot/profile", "at": "…"}
```

It sends these headers:

- `X-EAP-Timestamp`: Unix seconds.
- `X-EAP-Event`.
- `X-EAP-Signature: sha256=<hex>`: an HMAC-SHA256 with the webhook secret over `"<timestamp>." + raw body`.

Reject messages older than 5 minutes. Reference implementation: `eap_governor.external.verify`.

## 4. Copilot Studio walkthrough

This uses the HR agent as an example.

1. **Governor**: create the agent `hr-copilot` and write its functional context in the studio:
   - role, tone, boundaries
   - hand-over triggers
   - personal data: redact

   Then, under Technical setup → Where this agent runs, set:
   - platform: Copilot Studio
   - secret name: `DIRECTLINE_HR_COPILOT`
   - allowed caller: the connector app id
   - webhook: the Power Automate URL from step 4
2. **Custom connector** in Power Platform for the edge URL, with OAuth 2.0 (Entra ID, client
   credentials of the caller app, resource `api://<eap-app-id>`). Add three actions:
   `GetProfile`, `CheckInput` and `CheckOutput`.
3. **Copilot Studio topics**:
   - *Conversation Start*: call `GetProfile` for the context and keep `instructions`. Paste them into
     the agent's Instructions once per release, or set them from the flow.
   - Before generating an answer, call `CheckInput`. If `decision` ≠ `allow`, send `message` and end
     the topic. Otherwise generate the answer from `text`.
   - Call `CheckOutput` on the answer and send the returned `output`.
4. **Power Automate flow** "When an HTTP request is received" as the change webhook. Verify the
   signature, then re-read the profile and update the agent's instructions, or notify the maker.
5. **Release in the governor**: add tests (or turn 👎 answers from Monitor into tests), run
   Release → ① Test. The tests chat with the published Copilot Studio bot over Direct Line. Then
   approve and go live.

Alternatively, Copilot Studio can call **platform-hosted** agents as tools over MCP (`/mcp` on the
edge; set *Expose over MCP* on those agents). Use that when the reasoning should run here rather than
in Copilot Studio.

## 5. Not verified yet

These need checking against a real tenant:

- **Direct Line details**: replies are matched by `replyToId` and the conversation is read by polling
  activities. The functional context is sent as `channelData.eap_context`, and a Copilot Studio topic
  has to read it to switch context.
- **Power Platform specifics**: the connector, topic and flow steps above follow the documented
  building blocks, but have not been run end to end.
- **A2A**: the A2A protocol is not implemented; A2A agents are tested like HTTP agents.
- **Entra ID issuer**: only v2 tokens (`login.microsoftonline.com/<tenant>/v2.0`) match the configured
  issuer. v1 tokens (`sts.windows.net`) are rejected.
