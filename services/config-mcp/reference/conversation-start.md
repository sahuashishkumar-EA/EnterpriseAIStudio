# Reference conversation-start step for low-code agents (C08-F4)

Target: Agent Studio / Agent Designer in the Gemini Enterprise app (fallback: a Conversational
Agents playbook). Confirm in the pilot that the builder can run this deterministically (spec 12).

1. Add the MCP server `eap-config` (URL behind Agent Gateway, agent identity auth) as a tool.
2. First step of every conversation, before any model turn:
   - call `get_active_profile` with `agent_id = <this agent's id>` and `business_unit = <user's BU>`
   - store `persona`, `instructions`, `output_format`, `thresholds`, `escalation_contacts`,
     `versions` in session variables
3. The agent's static instructions (the stable "shell") only say: follow `$instructions`, speak as
   `$persona`, answer in `$output_format`; escalate to `$escalation_contacts`.
4. Log `$versions` with every turn (the builder's custom attributes / trace labels).

Thresholds are for display only. They are enforced by the domain MCP servers (C12), never by the model.
