"""BU variants of a core agent.

A core agent is owned by its technical team (system prompt, orchestration, tools, guardrails).
Its `customization` says what business units may change. A BU variant is an agent of its own
(owned by the BU, with its own drafts, tests, approvals and versions) that `extends` one published
core version and carries:

  * its own functional contexts (audiences, tone, boundaries, web knowledge, starters)
  * an `overlay`: BU rules appended to the core prompt, model choice, I/O contract, output format,
    tighter limits, extra guardrails and knowledge - each only if the core allows it

The effective definition (what the runtime executes) = compose(core version, variant). The runtime
never sees the split: the governor composes before resolving, previewing and testing.
"""

from __future__ import annotations

from typing import Any

from .schema import AgentDefinition, Customization, Overlay

CLASSIFICATION_ORDER = ["public", "internal", "confidential", "restricted"]
LIMIT_KEYS = ("max_cost_usd", "max_input_tokens", "max_output_tokens", "max_latency_ms", "max_tool_calls",
              "max_iterations", "max_depth")


def _min(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def compose(core: AgentDefinition, variant: AgentDefinition) -> AgentDefinition:
    o = variant.overlay
    data = core.model_dump(mode="json")
    instructions = core.prompt.instructions
    if o.instructions.strip():
        instructions = (instructions + "\n\n" if instructions else "") + \
            f"## Rules of business unit {variant.owner_bu}\n{o.instructions.strip()}"
    data.update(
        name=variant.name,
        owner_bu=variant.owner_bu,
        description=variant.description or core.description,
        data_classification=max(core.data_classification, variant.data_classification, key=CLASSIFICATION_ORDER.index),
        exposure=variant.exposure.model_dump(mode="json"),
        contexts=[c.model_dump(mode="json") for c in variant.contexts],
        default_context=variant.default_context,
        tags=list(dict.fromkeys([*core.tags, *variant.tags])),
        customization=Customization().model_dump(mode="json"),
        extends=None,
        overlay=Overlay().model_dump(mode="json"),
        metadata={**core.metadata, **variant.metadata,
                  "variant_of": {"agent": core.name, "version": variant.extends.version if variant.extends else None,
                                 "content_hash": core.content_hash()}},
    )
    data["prompt"]["instructions"] = instructions
    if o.model is not None:
        data["model"] = o.model.model_dump(mode="json")
    if o.contract is not None:
        data["contract"] = o.contract.model_dump(mode="json")
    if o.output is not None:
        data["output"] = o.output.model_dump(mode="json")
    data["limits"] = {k: _min(getattr(core.limits, k), getattr(o.limits, k)) for k in LIMIT_KEYS}
    data["guardrails"] = [*data["guardrails"], *(g.model_dump(mode="json") for g in o.guardrails)]
    data["bindings"]["knowledge"] = [*data["bindings"]["knowledge"], *(k.model_dump(mode="json") for k in o.knowledge)]
    return AgentDefinition.model_validate(data)


def check(core: AgentDefinition, variant: AgentDefinition) -> list[dict[str, str]]:
    """What the variant changes that the core does not allow (empty = fine)."""
    out: list[dict[str, str]] = []

    def bad(code: str, path: str, msg: str) -> None:
        out.append({"code": code, "path": path, "message": msg})

    cz, o = core.customization, variant.overlay
    if core.extends is not None:
        bad("variant.chain", "extends", f"{core.name} is itself a variant; extend the core agent instead")
        return out
    if not cz.enabled:
        bad("variant.disabled", "extends", f"{core.name} does not allow business-unit variants (Customization in its technical setup)")
        return out
    if "*" not in cz.allowed_bus and variant.owner_bu not in cz.allowed_bus:
        bad("variant.bu", "owner_bu", f"business unit {variant.owner_bu} may not customize {core.name}")
    if o.instructions.strip() and not cz.instructions:
        bad("variant.instructions", "overlay.instructions", "the core does not allow BU rules in the prompt")
    if o.model is not None and o.model.catalog_id != core.model.catalog_id and o.model.catalog_id not in cz.models:
        allowed = ", ".join([core.model.catalog_id, *cz.models])
        bad("variant.model", "overlay.model", f"model {o.model.catalog_id} is not offered to BUs (allowed: {allowed})")
    if o.model is not None:
        for fb in o.model.fallbacks:
            if fb not in cz.models and fb not in (core.model.catalog_id, *core.model.fallbacks):
                bad("variant.model", "overlay.model.fallbacks", f"fallback model {fb} is not offered to BUs")
    if o.contract is not None and not cz.contract:
        bad("variant.contract", "overlay.contract", "the core does not allow a BU-specific input/output contract")
    if o.output is not None and not cz.output:
        bad("variant.output", "overlay.output", "the core does not allow changing the output format")
    if (o.knowledge or any(c.web_sources for c in variant.contexts)) and not cz.knowledge:
        bad("variant.knowledge", "overlay.knowledge", "the core does not allow BU knowledge sources or web scans")
    if any(c.web_search.enabled for c in variant.contexts) and not cz.web_search:
        bad("variant.web_search", "contexts", "the core does not allow live web search")
    for k in LIMIT_KEYS:
        mine, theirs = getattr(o.limits, k), getattr(core.limits, k)
        if mine is not None and theirs is not None and mine > theirs:
            bad("variant.limits", f"overlay.limits.{k}", f"{k} {mine} is above the core's {theirs} (variants can only tighten)")
    return out


def bootstrap(core: AgentDefinition, core_version: str, name: str, bu: str) -> dict[str, Any]:
    """A new variant draft: the core's contexts as a starting point, nothing overridden yet."""
    d = core.model_dump(mode="json")
    d.update(name=name, owner_bu=bu, extends={"agent": core.name, "version": core_version},
             overlay=Overlay().model_dump(mode="json"), customization=Customization().model_dump(mode="json"),
             description=f"{core.description} ({bu})".strip() if core.description else "",
             exposure={**d["exposure"], "chat": True})
    for c in d["contexts"]:
        c["web_sources"] = c.get("web_sources") or []
    return d
