package eap.registry_test

import rego.v1

import data.eap.registry

test_clean_profile_allowed if {
	count(registry.deny) == 0 with input as {"environment": "dev", "versions": [{"type": "profile", "name": "p", "content": {"instructions": "Focus on buying signals."}}]}
}

test_iban_denied if {
	count(registry.deny) == 1 with input as {"environment": "dev", "versions": [{"type": "profile", "name": "p", "content": {"instructions": "Pay to NL91ABNA0417164300"}}]}
}

test_high_risk_needs_identity if {
	count(registry.deny) == 1 with input as {"environment": "dev", "versions": [{"type": "agent", "name": "a", "content": {"riskTier": "high"}}]}
}
