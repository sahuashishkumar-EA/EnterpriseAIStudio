# ADR-002: Gemini Enterprise Agent Platform as the first real adapter target

Status: accepted, 2026-11-19

## Context

Spec open question 1 asks which platform the first real C10 adapter targets. The deployment target is GCP, and Google rebranded Vertex AI as Gemini Enterprise Agent Platform in April 2026 (Agent Runtime, Agent Studio / Agent Designer, Agent Identity, Agent Registry, Agent Gateway).

## Decision

Default the first real adapter to Agent Platform: pro-code agents on Agent Runtime (ADK + C09 SDK), low-code agents in Agent Studio with the C08 conversation-start step, tools behind Agent Gateway or Apigee MCP proxies, knowledge in RAG Engine / Vertex AI Search, guardrails as Model Armor templates. The mock adapter stays the CI default. Skeleton: `services/compiler/eap_compiler/adapters/agent_platform.py`.

## Consequences

Vendor names stay inside the adapter and Terraform (principle 7). Regional availability in europe-west4 must be confirmed before production.
