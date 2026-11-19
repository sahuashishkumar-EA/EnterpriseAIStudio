# Policy check run by the promotion workflow (C05-F1 step 2) against OPA:
#   POST /v1/data/eap/registry/deny   {"input": {...}}   -> {"result": ["reason", ...]}
# Input: {"environment", "requested_by", "versions": [{"type", "name", "content", "cold_paths"}]}
# Keep in step with services/workflow/eap_workflow/policy.py (LocalPolicy mirror, ADR-006).
package eap.registry

import rego.v1

iban_like := `[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}`

email_like := `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`

free_text(v) := concat(" ", array.concat([object.get(v.content, "instructions", "")], [e.input | some e in object.get(v.content, "examples", [])]))

# Personal data never stored in configuration (spec 11, data protection).
deny contains msg if {
	some v in input.versions
	v.type in {"profile", "skill"}
	regex.match(iban_like, free_text(v))
	msg := sprintf("%s %s: instructions contain what looks like an IBAN", [v.type, v.name])
}

deny contains msg if {
	some v in input.versions
	v.type in {"profile", "skill"}
	regex.match(email_like, free_text(v))
	msg := sprintf("%s %s: instructions contain an e-mail address; use escalationContacts", [v.type, v.name])
}

# High-risk agents must run with their own identity.
deny contains msg if {
	some v in input.versions
	v.type == "agent"
	v.content.riskTier == "high"
	not v.content.identityRef
	msg := sprintf("agent %s: high-risk agents need identityRef", [v.name])
}

# Production tools are reached over TLS only.
deny contains msg if {
	input.environment == "production"
	some v in input.versions
	v.type == "tool_def"
	not startswith(object.get(v.content, "endpoint", "https://"), "https://")
	msg := sprintf("tool_def %s: endpoint must use https in production", [v.name])
}
