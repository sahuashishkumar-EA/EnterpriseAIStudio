// BU variants of a core agent.
// Core (technical owners): "Business-unit customization" in the technical setup + a variants panel.
// Variant (BU functional experts): banner with the core version, "BU settings" tab instead of Technical.
"use strict";

function isVariant(agent) { return !!agent.variant_of; }

// Shown above the tabs of a variant.
function variantBanner(agent) {
  if (!isVariant(agent)) return "";
  const ext = agent.draft.definition.extends || {};
  const core = agent.core || {};
  const newer = core.live_version && core.live_version !== ext.version;
  return h("div", { class: "variant-banner" + (newer ? " warn" : "") },
    h("span", {}, "🏢 Variant for ", h("b", {}, agent.owner_bu), " of ", h("a", { href: `#/agents/${core.agent}` }, core.agent),
      " · built on core ", h("code", {}, ext.version), core.owner_bu ? h("span", { class: "muted" }, ` (core owned by ${core.owner_bu})`) : ""),
    newer ? h("span", {}, ` · core ${core.live_version} is live. `,
      agent.permissions && agent.permissions.functional_author ? h("button", { class: "small-btn primary", onclick: () => act(async () => {
        await api("POST", `/agents/${agent.name}/rebase`, {});
        toast(`Draft moved to core ${core.live_version}. Run the tests in Release, then go live.`); route();
      }) }, `Update to core ${core.live_version}`) : "") : "");
}

// ---------------------------------------------------------------- core: customization section

function sectionCustomization(def) {
  def.customization = def.customization || { enabled: false, allowed_bus: ["*"], models: [] };
  const cz = def.customization;
  const body = h("div", {});
  const tick = (key, label, hint) => {
    const c = h("input", { type: "checkbox", checked: cz[key] !== false && (cz[key] || ["instructions", "output", "knowledge"].includes(key)) });
    cz[key] = c.checked; c.onchange = () => (cz[key] = c.checked);
    return h("label", { class: "toggle" }, c, h("span", {}, label, hint ? h("span", { class: "muted small" }, " — " + hint) : ""));
  };
  const render = () => {
    if (!cz.enabled) { body.replaceChildren(h("p", { class: "muted" }, "Only this agent exists. Turn on to let business units create their own variant of it.")); return; }
    const bus = h("input", { value: (cz.allowed_bus || ["*"]).join(", "), placeholder: "* = every BU, or e.g. retail, wealth" });
    bus.onchange = () => (cz.allowed_bus = bus.value.split(",").map((s) => s.trim()).filter(Boolean));
    const models = h("div", { class: "model-picks" }, state.catalog.models.filter((m) => m.id !== def.model.catalog_id && m.status !== "retired").map((m) => {
      const c = h("input", { type: "checkbox", checked: (cz.models || []).includes(m.id) });
      c.onchange = () => { cz.models = c.checked ? [...new Set([...(cz.models || []), m.id])] : (cz.models || []).filter((x) => x !== m.id); };
      return h("label", { class: "toggle" }, c, h("span", {}, m.id, " ", statusBadge(m.status)));
    }));
    body.replaceChildren(
      h("p", { class: "muted" }, "You keep the system prompt, orchestration, tools and guardrails. Each BU gets its own variant with its own audiences, behaviour, tests and release. These settings decide what else they may change."),
      field("Business units that may create a variant", bus),
      h("h3", {}, "Models BUs may choose (besides ", h("code", {}, def.model.catalog_id), ")"), models,
      h("h3", {}, "BUs may also"),
      tick("instructions", "add their own rules to the prompt", "appended after yours, never replacing it"),
      tick("output", "change the output format and citations"),
      tick("contract", "define their own input/output contract", "for BU-specific integrations"),
      tick("knowledge", "add knowledge sources and scan web pages"),
      tick("web_search", "let the agent search the web live"),
      h("p", { class: "muted small" }, "Limits can only be tightened by a BU, and BU guardrails only add to yours. Changes here apply to variants built on the next core version you publish."));
  };
  const on = h("input", { type: "checkbox", checked: !!cz.enabled });
  on.onchange = () => { cz.enabled = on.checked; render(); };
  render();
  return h("div", { class: "panel" }, h("h2", {}, "Business-unit customization"),
    h("label", { class: "toggle" }, on, h("span", {}, "Allow business units to create their own variant of this agent")), body);
}

// ---------------------------------------------------------------- core: variants panel

function variantsPanel(agent) {
  const d = agent.draft.definition;
  const cz = d.customization || {};
  const live = agent.aliases.find((a) => a.alias === "prod");
  const mine = Object.entries(state.me.roles || {}).filter(([bu, rs]) => bu !== "*" && rs.some((r) => ["functional_author", "author", "bu_admin"].includes(r))).map(([bu]) => bu);
  const isAdmin = (state.me.roles["*"] || []).includes("platform_admin");
  const taken = new Set((agent.variants || []).map((v) => v.bu));
  const candidates = (isAdmin ? [...new Set([...mine, ...((cz.allowed_bus || []).filter((b) => b !== "*"))])] : mine)
    .filter((b) => !taken.has(b) && b !== agent.owner_bu && ((cz.allowed_bus || ["*"]).includes("*") || cz.allowed_bus.includes(b)));
  const create = () => {
    const bu = select(candidates.map((b) => [b, b]), candidates[0], () => {});
    const name = h("input", { placeholder: `${agent.name}-<bu>` });
    const close = modal("Create a variant for your business unit",
      h("p", { class: "muted" }, `Your variant starts from ${agent.name} ${live ? live.version : ""} with its contexts. You then shape the behaviour for your BU, test it and release it yourself. The core team keeps the technical setup.`),
      field("Business unit", bu), field("Name (optional)", name),
      h("div", { class: "row-btns" }, h("button", { onclick: () => close() }, "Cancel"),
        h("button", { class: "primary", onclick: () => act(async () => {
          const r = await api("POST", `/agents/${agent.name}/variants`, { bu: bu.value, name: name.value.trim() || undefined });
          close(); toast(`Created ${r.name}`); location.hash = `#/agents/${r.name}/studio`;
        }) }, "Create variant")));
  };
  if (!cz.enabled && !(agent.variants || []).length) return "";
  return h("div", { class: "panel" }, h("div", { class: "row-head" }, h("h3", {}, "🏢 Business-unit variants"),
      cz.enabled && live && candidates.length ? h("button", { class: "primary", onclick: create }, "+ Variant for my BU") : ""),
    !live && cz.enabled ? h("p", { class: "muted small" }, "Publish the core first; variants build on a published version.") : "",
    (agent.variants || []).length ? h("table", {}, h("tr", {}, ...["Business unit", "Variant", "Live", "Built on core", "Model"].map((x) => h("th", {}, x))),
      agent.variants.map((v) => h("tr", {}, h("td", {}, v.bu), h("td", {}, h("a", { href: `#/agents/${v.name}` }, v.name)),
        h("td", {}, v.live_version || badge("not live")),
        h("td", {}, v.live_core_version || v.draft_core_version, live && (v.live_core_version || v.draft_core_version) !== live.version ? badge("older core", "warn") : ""),
        h("td", {}, v.model || h("span", { class: "muted" }, "core model"))))) :
      h("p", { class: "muted small" }, "No variants yet."));
}

// ---------------------------------------------------------------- variant: BU settings tab

function tabBuSettings(agent) {
  const def = structuredClone(agent.draft.definition);
  const core = agent.core || {};
  const cz = core.customization || {};
  def.overlay = def.overlay || {};
  const ov = def.overlay;
  ov.limits = ov.limits || {};
  let base = agent.draft.sections.bu, dirty = false;
  state.dirty = () => dirty;
  const can = !!(agent.permissions && agent.permissions.functional_author);
  const locked = (what) => h("p", { class: "muted small" }, `🔒 ${what} is decided by the core owners (${core.owner_bu}).`);
  const card = (title, hint, ...body) => h("div", { class: "panel" }, h("h2", {}, title), hint ? h("p", { class: "muted small" }, hint) : "", ...body);

  // rules
  const rules = h("textarea", { class: "prose", rows: 5, placeholder: "e.g. Always mention our Retail service number 0800-123. Prices are in EUR." });
  rules.value = ov.instructions || ""; rules.oninput = () => { ov.instructions = rules.value; dirty = true; };

  // model
  const modelOpts = [["", `Core model (${core.model})`], ...(cz.models || []).map((m) => [m, m])];
  const model = select(modelOpts, (ov.model || {}).catalog_id || "", (v) => { ov.model = v ? { catalog_id: v } : null; dirty = true; });

  // output
  const outFmt = select([["", "Same as core"], ["text", "Plain text"], ["markdown", "Markdown"], ["json", "JSON"]], (ov.output || {}).format || "", (v) => {
    ov.output = v ? { ...(core.output || {}), ...(ov.output || {}), format: v } : null; dirty = true; });
  const cite = h("input", { type: "checkbox", checked: !!(ov.output || core.output || {}).require_citations });
  cite.onchange = () => { ov.output = { ...(core.output || { format: "text" }), ...(ov.output || {}), require_citations: cite.checked }; dirty = true; };

  // contract
  const ownContract = h("input", { type: "checkbox", checked: !!ov.contract });
  const contractBox = h("div", {});
  const renderContract = () => {
    if (!ownContract.checked) { contractBox.replaceChildren(h("p", { class: "muted small" }, "Uses the core's contract.")); return; }
    ov.contract = ov.contract || structuredClone(core.contract || { input_schema: { type: "object", properties: {} }, output_schema: {} });
    contractBox.replaceChildren(
      h("h3", {}, "Input"), schemaBuilder(ov.contract.input_schema, (s) => { ov.contract.input_schema = s; dirty = true; }),
      h("h3", {}, "Output (structured answers)"), schemaBuilder(ov.contract.output_schema || {}, (s) => { ov.contract.output_schema = s; dirty = true; }));
  };
  ownContract.onchange = () => { if (!ownContract.checked) ov.contract = null; dirty = true; renderContract(); };
  renderContract();

  // limits (tighten only)
  const lim = h("table", {}, Object.entries(LIMIT_LABELS).filter(([k]) => ["max_cost_usd", "max_output_tokens", "max_latency_ms", "max_tool_calls"].includes(k)).map(([k, label]) => {
    const coreV = (core.limits || {})[k];
    const inp = h("input", { type: "number", value: ov.limits[k] ?? "", placeholder: coreV ?? "no limit", step: "any" });
    const msg = h("span", { class: "fail small" });
    inp.oninput = () => {
      const v = inp.value === "" ? null : Number(inp.value);
      if (v === null) delete ov.limits[k]; else ov.limits[k] = v;
      msg.textContent = v !== null && coreV != null && v > coreV ? ` ✗ above the core's ${coreV}` : "";
      dirty = true;
    };
    return h("tr", {}, h("td", {}, label), h("td", {}, inp, msg), h("td", { class: "muted small" }, coreV != null ? `core: ${coreV}` : "core: no limit"));
  }));

  // channels
  def.exposure = def.exposure || {};
  const ch = (key, label) => { const c = h("input", { type: "checkbox", checked: !!def.exposure[key] }); c.onchange = () => { def.exposure[key] = c.checked; dirty = true; }; return h("label", { class: "toggle" }, c, h("span", {}, label)); };

  const result = h("div", {});
  const save = () => act(async () => {
    try {
      const d = await api("PUT", `/agents/${agent.name}/draft/bu`, { overlay: ov, exposure: def.exposure, base });
      base = d.sections.bu; dirty = false; toast("BU settings saved");
    } catch (e) {
      if (e.status === 409) { toast("Someone else changed the BU settings. Reload to see their changes.", true); return; }
      throw e;
    }
  });
  const validate = () => act(async () => {
    const r = await api("POST", `/agents/${agent.name}/draft/validate`, { definition: def });
    result.replaceChildren(r.ok ? h("p", { class: "pass" }, "✓ Within what the core allows and within policy")
      : h("ul", { class: "blockers" }, r.violations.map((v) => h("li", {}, v.message))));
  });
  const ro = (el) => { if (!can) el.querySelectorAll("input, select, textarea, button").forEach((x) => (x.disabled = true)); return el; };

  return ro(h("div", {},
    h("p", { class: "muted" }, "What your business unit changes on top of the core agent. Audiences, tone, knowledge and boundaries are in the ",
      h("a", { href: `#/agents/${agent.name}/studio` }, "Functional studio"), ". Everything else (system prompt, orchestration, tools) comes from the core."),
    h("div", { class: "row panel" }, h("div", { class: "muted" }, can ? "Changes are saved to the draft; test and release them in 🚀 Release." : "Read-only: you are not a functional author of this BU."),
      h("div", { style: "flex:0 0 auto; display:flex; gap:8px" }, h("button", { onclick: validate }, "Check"), h("button", { class: "primary", onclick: save }, "Save BU settings"))),
    result,
    card("Business-unit rules", "Added to the core prompt for every conversation of your BU.", cz.instructions === false ? locked("The prompt") : rules),
    card("Language model", "The model your BU's conversations use. The core owners decide which models are offered.",
      (cz.models || []).length ? model : h("div", {}, h("p", {}, "Core model: ", h("code", {}, core.model)), locked("The model"))),
    card("Answer format", "", cz.output === false ? locked("The output format") : h("div", { class: "row" }, field("Format", outFmt),
      field("Sources", h("label", { class: "toggle" }, cite, h("span", {}, "require sources (citations) in every answer"))))),
    card("Interface (contract)", "Only needed when another system calls your variant with its own fields.",
      cz.contract ? h("div", {}, h("label", { class: "toggle" }, ownContract, h("span", {}, "use our own input/output contract")), contractBox) : locked("The contract")),
    card("Limits", "You can make limits stricter than the core, never looser.", lim),
    card("Channels", "", h("div", { class: "row" }, ch("chat", "Offer in the chat app"), ch("mcp", "Expose over MCP"), ch("callable_by_agents", "Callable by other agents")))));
}

window.variantBanner = variantBanner; window.sectionCustomization = sectionCustomization;
window.variantsPanel = variantsPanel; window.tabBuSettings = tabBuSettings; window.isVariant = isVariant;
