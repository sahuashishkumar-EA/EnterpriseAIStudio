// Governor console. Vanilla JS SPA over the /api/v1 endpoints.
"use strict";

const state = { plugins: null, catalog: null, me: null, settings: {} };

function persona() { try { return localStorage.getItem("eap.persona") || "functional"; } catch { return "functional"; } }
function setPersona(p) { try { localStorage.setItem("eap.persona", p); } catch {} }

// ------------------------------------------------------------------ api + identity

function devIdentity() {
  try { return JSON.parse(localStorage.getItem("eap.dev") || "null"); } catch { return null; }
}
function setDevIdentity(v) {
  try { v ? localStorage.setItem("eap.dev", JSON.stringify(v)) : localStorage.removeItem("eap.dev"); } catch {}
}

async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const dev = devIdentity();
  if (dev && dev.user) { headers["X-Dev-User"] = dev.user; if (dev.roles) headers["X-Dev-Roles"] = dev.roles; }
  const res = await fetch("/api/v1" + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const err = new Error((data.error && data.error.message) || res.statusText);
    err.details = data.error && data.error.details; err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, bad) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), bad ? 9000 : 3500);
}
function fail(e) {
  let msg = e.message;
  if (e.details) msg += "\n" + (typeof e.details === "string" ? e.details : JSON.stringify(e.details, null, 2)).slice(0, 1500);
  toast(msg, true);
}
async function act(fn) { try { return await fn(); } catch (e) { fail(e); } }

function renderIdentity() {
  const box = document.getElementById("identity");
  box.innerHTML = "";
  const dev = devIdentity() || {};
  const user = h("input", { placeholder: "dev user", value: dev.user || "" });
  const roles = h("input", { placeholder: "bu:role,…", value: dev.roles || "" });
  const per = persona();
  box.append(
    h("div", { class: "persona", title: "Which screens to start on. Both personas edit the same agent." },
      ...[["functional", "🧑‍💼 Functional"], ["technical", "🛠️ Technical"]].map(([k, l]) =>
        h("button", { class: "seg-opt" + (per === k ? " on" : ""), onclick: () => { setPersona(k); renderIdentity(); route(); } }, l))),
    state.settings.relaxed_release ? h("span", { class: "badge warn", title: "One person may author, approve and publish (audited)." }, "dev mode") : "",
    state.me ? h("span", {}, state.me.principal) : h("span", {}, "not signed in"),
    h("span", { title: "Dev mode only: sent as X-Dev-* headers. Ignored behind IAP." }, "dev:"), user, roles,
    h("button", { onclick: () => { setDevIdentity(user.value ? { user: user.value, roles: roles.value } : null); boot(); } }, "Use"));
}

async function loadShared() {
  state.me = await api("GET", "/me").catch(() => null);
  if (state.me) state.settings = await api("GET", "/settings").catch(() => ({}));
  renderIdentity();
  if (!state.me) return false;
  state.plugins = await api("GET", "/plugins");
  const [m, t, k] = await Promise.all(["models", "tools", "knowledge"].map((x) => api("GET", "/catalog/" + x)));
  state.catalog = { models: m.models, tools: t.tools, knowledge: k.knowledge };
  return true;
}

const pluginSpec = (kind, type) => (state.plugins[kind] || []).find((s) => s.type === type);
const badge = (text, kind) => h("span", { class: "badge " + (kind || "") }, text);
const statusBadge = (s) => badge(s, { active: "ok", ok: "ok", completed: "ok", preview: "warn", flagged: "warn", partial: "warn",
  queued: "warn", running: "warn", deprecated: "warn", retired: "bad", blocked: "bad", failed: "bad", error: "bad" }[s]);
const pre = (v) => h("pre", {}, typeof v === "string" ? v : JSON.stringify(v, null, 2));

// ------------------------------------------------------------------------ router

const routes = [
  [/^#\/agents\/([^/?]+)(?:\/(\w+))?(?:\?.*)?$/, (m) => viewAgent(decodeURIComponent(m[1]), m[2] || (persona() === "technical" ? "technical" : "overview"))],
  [/^#\/agents$/, viewAgents],
  [/^#\/catalog$/, viewCatalog],
  [/^#\/policies$/, viewPolicies],
  [/^#\/graph$/, viewGraph],
  [/^#\/roles$/, viewRoles],
  [/^#\/audit$/, viewAudit],
];

let ready = Promise.resolve();
async function route() {
  await ready; // never render a screen before identity, settings and catalog are loaded
  const view = document.getElementById("view");
  if (!state.me) {
    view.replaceChildren(h("div", { class: "panel" }, h("h1", {}, "Sign in"),
      h("p", {}, "Behind IAP you are signed in automatically. In local dev, enter a dev user and roles (e.g. ",
        h("code", {}, "*:platform_admin"), " or ", h("code", {}, "retail:author"), ") in the top bar.")));
    return;
  }
  const hash = location.hash || "#/agents";
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { view.replaceChildren(h("p", { class: "muted" }, "Loading…")); try { view.replaceChildren(await fn(m)); } catch (e) { fail(e); view.replaceChildren(h("p", { class: "fail" }, e.message)); } return; }
  }
  location.hash = "#/agents";
}

// ------------------------------------------------------------------------ agents

async function viewAgents() {
  const { agents } = await api("GET", "/agents");
  const name = h("input", { placeholder: "customer-profile" });
  const bu = h("input", { placeholder: "retail" });
  const desc = h("input", { placeholder: "What the agent does" });
  return h("div", {},
    h("h1", {}, "Agents"),
    h("div", { class: "panel" }, h("table", {},
      h("tr", {}, ["Name", "BU", "Latest", "Aliases", "Draft updated"].map((x) => h("th", {}, x))),
      agents.map((a) => h("tr", {},
        h("td", {}, h("a", { href: "#/agents/" + a.name }, a.name), h("div", { class: "muted" }, a.description)),
        h("td", {}, a.owner_bu), h("td", {}, a.latest_version || "—"),
        h("td", {}, Object.entries(a.aliases).map(([k, v]) => h("div", {}, h("code", {}, k), " → ", v))),
        h("td", { class: "muted" }, (a.draft_updated_at || "").slice(0, 19)))))),
    h("div", { class: "panel" }, h("h2", {}, "New agent"),
      h("div", { class: "row" }, h("div", {}, h("label", {}, "Name"), name), h("div", {}, h("label", {}, "Owner BU"), bu),
        h("div", {}, h("label", {}, "Description"), desc),
        h("div", {}, h("button", { class: "primary", onclick: () => act(async () => {
          await api("POST", "/agents", { name: name.value.trim(), owner_bu: bu.value.trim(), description: desc.value });
          location.hash = "#/agents/" + name.value.trim();
        }) }, "Create")))));
}

const TABS = [["overview", "Overview"], ["studio", "🧑‍💼 Functional studio"], ["technical", "🛠️ Technical setup"],
  ["evals", "Evals"], ["release", "Approve & publish"], ["versions", "Versions & aliases"], ["audit", "Audit"]];

async function viewAgent(name, tab) {
  const agent = await api("GET", "/agents/" + encodeURIComponent(name));
  window.onbeforeunload = null;
  const body = await ({ overview: tabOverview, studio: tabStudio, technical: tabDefinition, definition: tabDefinition,
    evals: tabEvals, release: tabRelease, versions: tabVersions, audit: tabAgentAudit }[tab] || tabOverview)(agent);
  return h("div", {},
    h("h1", {}, agent.name, " ", h("span", { class: "muted" }, "· ", agent.owner_bu)),
    h("div", { class: "tabs" }, TABS.map(([k, label]) => h("a", { href: `#/agents/${name}/${k}`, class: k === tab ? "active" : "" }, label))),
    body);
}

// --------------------------------------------------------------- definition editor

function tabDefinition(agent) {
  const def = structuredClone(agent.draft.definition);
  let base = agent.draft.sections.technical;
  const status = h("div", { class: "muted" }, "Draft ", h("code", {}, agent.draft.content_hash.slice(0, 19)), " · edited by ", agent.draft.editors.join(", ") || "—");
  const result = h("div", {});
  let jsonMode = false;
  const container = h("div", {});

  const render = () => container.replaceChildren(jsonMode ? jsonEditor() : formEditor());
  const jsonEditor = () => {
    const ta = h("textarea", { rows: 32 }); ta.value = JSON.stringify(def, null, 2);
    ta.addEventListener("change", () => { try { Object.keys(def).forEach((k) => delete def[k]); Object.assign(def, JSON.parse(ta.value)); ta.style.borderColor = ""; } catch { ta.style.borderColor = "var(--bad)"; } });
    return h("div", { class: "panel" }, ta);
  };
  const formEditor = () => h("div", {}, sectionBasics(def), sectionPrompt(def), sectionModel(def), sectionPattern(def),
    sectionBindings(def), sectionGuardrails(def), sectionLimits(def), sectionOutput(def), sectionContract(def));

  const save = () => act(async () => {
    let d;
    try {
      d = await api("PUT", `/agents/${agent.name}/draft/technical`, { definition: def, base });
    } catch (e) {
      if (e.status === 409) { toast("Someone else changed the technical setup since you opened it. Reload to see their changes.", true); return; }
      throw e;
    }
    base = d.sections.technical;
    toast("Technical setup saved · " + d.content_hash.slice(0, 19));
    status.replaceChildren("Draft ", h("code", {}, d.content_hash.slice(0, 19)), " · edited by ", d.editors.join(", "));
  });
  const validate = () => act(async () => {
    const r = await api("POST", `/agents/${agent.name}/draft/validate`, { definition: def });
    result.replaceChildren(r.ok ? h("p", { class: "pass" }, "✓ Valid against schema, policy and catalog")
      : h("div", {}, h("p", { class: "fail" }, `${r.violations.length} violation(s)`),
        h("table", {}, r.violations.map((v) => h("tr", {}, h("td", {}, h("code", {}, v.code)), h("td", {}, v.path), h("td", {}, v.message))))));
  });
  render();
  return h("div", {},
    h("p", { class: "muted" }, "System-level context, model, orchestration, tools, technical guardrails, limits and output. ",
      "Functional contexts (role, behaviour, knowledge, boundaries) are edited in the ", h("a", { href: `#/agents/${agent.name}/studio` }, "Functional studio"),
      " — saving here never overwrites them."),
    h("div", { class: "row panel" }, status,
      h("div", { style: "flex:0 0 auto; display:flex; gap:8px" },
        h("button", { onclick: () => { jsonMode = !jsonMode; render(); } }, "Toggle JSON"),
        h("button", { onclick: validate }, "Validate"),
        h("button", { class: "primary", onclick: save }, "Save draft"))),
    result, container);
}

const field = (label, el) => h("div", {}, h("label", {}, label), el);
function bind(obj, key, el, cast) {
  el.value = obj[key] ?? "";
  el.addEventListener("change", () => { const v = cast ? cast(el.value) : el.value; if (v === "" || v === undefined) delete obj[key]; else obj[key] = v; });
  return el;
}
function select(options, value, onChange) {
  const s = h("select", {}, options.map(([v, l]) => h("option", { value: v }, l)));
  s.value = value ?? ""; s.addEventListener("change", () => onChange(s.value)); return s;
}

function sectionBasics(def) {
  return h("div", { class: "panel" }, h("h2", {}, "Basics"),
    h("div", { class: "row" },
      field("Description", bind(def, "description", h("input", {}))),
      field("Data classification", select(["public", "internal", "confidential", "restricted"].map((x) => [x, x]),
        def.data_classification || "internal", (v) => (def.data_classification = v))),
      field("Tags", (() => { const i = h("input", { value: (def.tags || []).join(", ") }); i.onchange = () => (def.tags = i.value.split(",").map((s) => s.trim()).filter(Boolean)); return i; })())),
    h("div", { class: "row" },
      field("Expose over MCP", (() => { def.exposure = def.exposure || {}; const c = h("input", { type: "checkbox", checked: def.exposure.mcp }); c.onchange = () => (def.exposure.mcp = c.checked); return c; })()),
      field("Callable by other agents", (() => { def.exposure = def.exposure || {}; const c = h("input", { type: "checkbox", checked: def.exposure.callable_by_agents !== false }); c.onchange = () => (def.exposure.callable_by_agents = c.checked); return c; })()),
      field("Offer in chat app", (() => { def.exposure = def.exposure || {}; const c = h("input", { type: "checkbox", checked: def.exposure.chat }); c.onchange = () => (def.exposure.chat = c.checked); return c; })())));
}

function sectionPrompt(def) {
  def.prompt = def.prompt || { system: "" };
  def.prompt.context = def.prompt.context || [];
  const list = h("div", {});
  const renderCtx = () => list.replaceChildren(...def.prompt.context.map((c, i) => h("div", { class: "list-item" },
    h("button", { class: "remove danger", onclick: () => { def.prompt.context.splice(i, 1); renderCtx(); } }, "Remove"),
    field("Title", bind(c, "title", h("input", {}))), field("Text", bind(c, "text", h("textarea", { class: "prose", rows: 4 }))))));
  renderCtx();
  return h("div", { class: "panel" }, h("h2", {}, "System-level context ", h("span", { class: "muted" }, "(technical; applies to every functional context)")),
    field("System prompt", bind(def.prompt, "system", h("textarea", { class: "prose", rows: 6 }))),
    field("Instructions", bind(def.prompt, "instructions", h("textarea", { class: "prose", rows: 3 }))),
    h("h3", {}, "Static context"), list,
    h("button", { onclick: () => { def.prompt.context.push({ title: "", text: "" }); renderCtx(); } }, "+ Context item"));
}

function sectionModel(def) {
  def.model = def.model || { catalog_id: "" };
  const opts = state.catalog.models.map((m) => [m.id, `${m.id} — ${m.provider}/${m.model_id} @ ${m.location} [${m.status}]`]);
  const fb = h("input", { value: (def.model.fallbacks || []).join(", "), placeholder: "catalog ids, comma separated" });
  fb.onchange = () => (def.model.fallbacks = fb.value.split(",").map((s) => s.trim()).filter(Boolean));
  return h("div", { class: "panel" }, h("h2", {}, "Model"),
    h("div", { class: "row" },
      field("Catalog model", select([["", "—"], ...opts], def.model.catalog_id, (v) => (def.model.catalog_id = v))),
      field("Fallbacks", fb)),
    field("Generation params (JSON, e.g. {\"max_output_tokens\": 2048, \"effort\": \"medium\"})",
      jsonArea(def.model.params || {}, (v) => (def.model.params = v || {}), 3)));
}

function sectionPattern(def) {
  def.pattern = def.pattern || { type: "single_shot", params: {} };
  const paramsBox = h("div", {});
  const renderParams = () => {
    const spec = pluginSpec("pattern", def.pattern.type);
    paramsBox.replaceChildren(spec ? h("div", {}, h("p", { class: "muted" }, spec.description),
      schemaForm(spec.params_schema, def.pattern.params, (v) => (def.pattern.params = v || {}))) : "");
  };
  renderParams();
  return h("div", { class: "panel" }, h("h2", {}, "Orchestration pattern"),
    field("Pattern", select(state.plugins.pattern.map((p) => [p.type, p.title]), def.pattern.type,
      (v) => { def.pattern = { type: v, params: {} }; renderParams(); })), paramsBox);
}

function sectionBindings(def) {
  def.bindings = def.bindings || {};
  const b = def.bindings; b.tools = b.tools || []; b.knowledge = b.knowledge || []; b.agents = b.agents || [];
  const tools = h("div", {}), know = h("div", {}), agents = h("div", {});
  const rt = () => tools.replaceChildren(...b.tools.map((t, i) => h("div", { class: "list-item row" },
    field("Tool", select([["", "—"], ...state.catalog.tools.map((x) => [x.id, `${x.id} (${x.kind}, ${x.data_classification})`])], t.ref, (v) => (t.ref = v))),
    field("Name exposed to model", bind(t, "name", h("input", { placeholder: t.ref }))),
    h("div", { style: "flex:0 0 auto" }, h("button", { class: "danger", onclick: () => { b.tools.splice(i, 1); rt(); } }, "Remove")))));
  const rk = () => know.replaceChildren(...b.knowledge.map((k, i) => h("div", { class: "list-item row" },
    field("Knowledge source", select([["", "—"], ...state.catalog.knowledge.map((x) => [x.id, `${x.id} (${x.data_classification})`])], k.ref, (v) => (k.ref = v))),
    field("top_k", bind(k, "top_k", h("input", { type: "number" }), Number)),
    field("min_score", bind(k, "min_score", h("input", { type: "number", step: "any" }), Number)),
    h("div", { style: "flex:0 0 auto" }, h("button", { class: "danger", onclick: () => { b.knowledge.splice(i, 1); rk(); } }, "Remove")))));
  const ra = () => agents.replaceChildren(...b.agents.map((a, i) => h("div", { class: "list-item row" },
    field("Agent", bind(a, "name", h("input", {}))), field("Alias / version", bind(a, "alias", h("input", { placeholder: "prod" }))),
    field("Description for the model", bind(a, "description", h("input", {}))),
    h("div", { style: "flex:0 0 auto" }, h("button", { class: "danger", onclick: () => { b.agents.splice(i, 1); ra(); } }, "Remove")))));
  rt(); rk(); ra();
  return h("div", { class: "panel" }, h("h2", {}, "Bindings"),
    h("h3", {}, "Tools"), tools, h("button", { onclick: () => { b.tools.push({ ref: "" }); rt(); } }, "+ Tool"),
    h("h3", {}, "Knowledge"), know, h("button", { onclick: () => { b.knowledge.push({ ref: "", top_k: 4 }); rk(); } }, "+ Knowledge"),
    h("h3", {}, "Agents (called on behalf of the same user, budget and depth inherited)"), agents,
    h("button", { onclick: () => { b.agents.push({ name: "", alias: "prod" }); ra(); } }, "+ Agent"));
}

function sectionGuardrails(def) {
  def.guardrails = def.guardrails || [];
  const list = h("div", {});
  const r = () => list.replaceChildren(...def.guardrails.map((g, i) => {
    const spec = pluginSpec("guardrail", g.type);
    const params = h("div", {});
    const rp = () => { const s = pluginSpec("guardrail", g.type); params.replaceChildren(s ? schemaForm(s.params_schema, g.params, (v) => (g.params = v || {})) : ""); };
    rp();
    return h("div", { class: "list-item" },
      h("button", { class: "remove danger", onclick: () => { def.guardrails.splice(i, 1); r(); } }, "Remove"),
      h("div", { class: "row" },
        field("Type", select(state.plugins.guardrail.map((p) => [p.type, p.title]), g.type, (v) => { g.type = v; g.params = {}; const s = pluginSpec("guardrail", v); if (s && !s.stages.includes(g.stage)) g.stage = s.stages[0]; r(); })),
        field("Stage", select((spec ? spec.stages : []).map((s) => [s, s]), g.stage, (v) => (g.stage = v))),
        field("Action", select([["block", "block"], ["redact", "redact"], ["warn", "warn"]], g.action || "block", (v) => (g.action = v)))),
      params);
  }));
  r();
  return h("div", { class: "panel" }, h("h2", {}, "Guardrails ", h("span", { class: "muted" }, "(hard controls; platform/BU-required guardrails are always added)")),
    list, h("button", { onclick: () => { def.guardrails.push({ type: "pii_redaction", stage: "output", action: "redact", params: {} }); r(); } }, "+ Guardrail"));
}

function sectionLimits(def) {
  def.limits = def.limits || {};
  const keys = ["max_cost_usd", "max_input_tokens", "max_output_tokens", "max_latency_ms", "max_tool_calls", "max_iterations", "max_depth"];
  return h("div", { class: "panel" }, h("h2", {}, "Limits ", h("span", { class: "muted" }, "(capped by effective policy)")),
    h("div", { class: "row" }, keys.map((k) => field(k, bind(def.limits, k, h("input", { type: "number", step: "any" }), (v) => (v === "" ? undefined : Number(v)))))));
}

function sectionOutput(def) {
  def.output = def.output || { format: "text" };
  const c = h("input", { type: "checkbox", checked: def.output.require_citations }); c.onchange = () => (def.output.require_citations = c.checked);
  return h("div", { class: "panel" }, h("h2", {}, "Output format"),
    h("div", { class: "row" }, field("Format", select([["text", "text"], ["json", "json"], ["markdown", "markdown"]], def.output.format, (v) => (def.output.format = v))),
      field("Require citations", c)),
    field("JSON schema (for json format)", jsonArea(def.output.json_schema ?? undefined, (v) => (def.output.json_schema = v ?? null), 6)));
}

function sectionContract(def) {
  def.contract = def.contract || {};
  return h("div", { class: "panel" }, h("h2", {}, "Contract ", h("span", { class: "muted" }, "(breaking changes force a major version)")),
    field("Description", bind(def.contract, "description", h("input", {}))),
    h("div", { class: "row" },
      field("Input schema", jsonArea(def.contract.input_schema, (v) => (def.contract.input_schema = v || {}), 8)),
      field("Output schema", jsonArea(def.contract.output_schema, (v) => (def.contract.output_schema = v || {}), 8))));
}

// ---------------------------------------------------------------------------- evals

async function tabEvals(agent) {
  const [{ suites }, { runs }] = await Promise.all([api("GET", `/agents/${agent.name}/evals/suites`), api("GET", `/agents/${agent.name}/evals/runs`)]);
  const detail = h("div", {});
  const sname = h("input", { placeholder: "smoke" });
  const fmt = select([["json", "JSON"], ["jsonl", "JSONL"], ["csv", "CSV"]], "json", () => {});
  const content = h("textarea", { rows: 8, placeholder: '[{"id": "c1", "input": {"text": "..."}, "expected": {...}}]' });
  const file = h("input", { type: "file", accept: ".json,.jsonl,.csv" });
  file.addEventListener("change", async () => {
    const f = file.files[0]; if (!f) return;
    content.value = await f.text(); fmt.value = f.name.split(".").pop().toLowerCase();
    if (!sname.value) sname.value = f.name.replace(/\.\w+$/, "");
  });
  let assertions = [{ type: "status", equals: "ok" }];
  const aBox = h("div", {});
  const ra = () => aBox.replaceChildren(...assertions.map((a, i) => {
    const params = Object.assign({}, a); delete params.type;
    const spec = pluginSpec("assertion", a.type);
    return h("div", { class: "list-item" },
      h("button", { class: "remove danger", onclick: () => { assertions.splice(i, 1); ra(); } }, "Remove"),
      field("Assertion", select(state.plugins.assertion.map((p) => [p.type, p.title]), a.type, (v) => { assertions[i] = { type: v }; ra(); })),
      spec ? schemaForm(spec.params_schema, params, (v) => { assertions[i] = Object.assign({ type: a.type }, v || {}); }) : "");
  }));
  ra();
  const required = h("input", { type: "checkbox", checked: true });
  return h("div", {},
    h("div", { class: "panel" }, h("h2", {}, "Suites"),
      suites.length ? h("table", {}, h("tr", {}, ["Suite", "Cases", "Assertions", "Required", ""].map((x) => h("th", {}, x))),
        suites.map((s) => h("tr", {}, h("td", {}, s.name), h("td", {}, s.cases.length),
          h("td", {}, s.assertions.map((a) => h("div", {}, h("code", {}, a.type), " ", a.path || ""))),
          h("td", {}, s.required ? "yes" : "no"),
          h("td", {}, h("button", { class: "danger", onclick: () => act(async () => { await api("DELETE", `/agents/${agent.name}/evals/suites/${s.id}`); route(); }) }, "Delete")))))
        : h("p", { class: "muted" }, "No suites yet. Publishing requires at least one required suite passing on the draft's content hash."),
      h("div", { style: "margin-top:10px; display:flex; gap:8px" },
        h("button", { class: "primary", onclick: () => act(async () => {
          toast("Running evals on the current draft…");
          const r = await api("POST", `/agents/${agent.name}/evals/run?wait=true`, { target: "draft" });
          toast(r.runs.map((x) => `${x.status}: ${x.passed}/${x.total}`).join(", ")); route();
        }) }, "Run all suites on draft"))),
    h("div", { class: "panel" }, h("h2", {}, "Add or replace a suite"),
      h("div", { class: "row" }, field("Name", sname), field("Format", fmt), field("Upload file", file), field("Required for release", required)),
      field("Cases", content),
      h("h3", {}, "Suite-level assertions (applied to every case)"), aBox,
      h("button", { onclick: () => { assertions.push({ type: "contains", path: "output" }); ra(); } }, "+ Assertion"), " ",
      h("button", { class: "primary", onclick: () => act(async () => {
        const r = await api("POST", `/agents/${agent.name}/evals/suites`, { name: sname.value, content: content.value, format: fmt.value, assertions, required: required.checked });
        toast(`Saved suite ${r.name} (${r.cases} cases)`); route();
      }) }, "Save suite")),
    h("div", { class: "panel" }, h("h2", {}, "Runs"),
      h("table", {}, h("tr", {}, ["Started", "Suite", "Target", "Hash", "Status", "Result", "Cost"].map((x) => h("th", {}, x))),
        runs.map((r) => h("tr", {}, h("td", {}, h("a", { href: "javascript:void 0", onclick: () => act(async () => detail.replaceChildren(renderRun(await api("GET", "/evals/runs/" + r.id)))) }, r.started_at.slice(0, 19))),
          h("td", {}, r.suite), h("td", {}, r.target, r.target_version ? "@" + r.target_version : ""),
          h("td", {}, h("code", {}, r.content_hash.slice(7, 19)), r.content_hash === agent.draft.content_hash ? " (current)" : ""),
          h("td", {}, statusBadge(r.status)), h("td", {}, `${r.passed}/${r.total} (${Math.round(r.pass_rate * 100)}%)`),
          h("td", {}, "$" + (r.cost_usd || 0).toFixed(4)))))),
    detail);
}

function renderRun(run) {
  return h("div", { class: "panel" }, h("h2", {}, "Run ", run.id),
    run.error ? h("p", { class: "fail" }, run.error) : "",
    h("table", {}, h("tr", {}, ["Case", "Status", "Assertions", "Output"].map((x) => h("th", {}, x))),
      run.results.map((c) => h("tr", {}, h("td", { class: c.passed ? "pass" : "fail" }, (c.passed ? "✓ " : "✗ ") + c.case_id),
        h("td", {}, statusBadge(c.status || "error")),
        h("td", {}, c.assertions.map((a) => h("div", { class: a.passed ? "pass" : "fail" }, `${a.passed ? "✓" : "✗"} ${a.type}${a.path ? " " + a.path : ""}: ${a.detail}`))),
        h("td", {}, pre(c.output ?? c.error ?? ""))))));
}

// ---------------------------------------------------------------- approve & publish

async function tabRelease(agent) {
  const chk = await api("GET", `/agents/${agent.name}/release-check`);
  const comment = h("input", { placeholder: "Review comment" });
  const changelog = h("input", { placeholder: "What changed" });
  const alias = h("input", { placeholder: "optional, e.g. staging" });
  const bump = select([["", `auto (${chk.required_bump})`], ["patch", "patch"], ["minor", "minor"], ["major", "major"]], "", () => {});
  return h("div", {},
    h("div", { class: "panel" }, h("h2", {}, "Release gate ", chk.ok ? badge("ready", "ok") : badge("blocked", "bad")),
      h("p", {}, "Draft ", h("code", {}, chk.content_hash.slice(0, 19)), " → next version ", h("b", {}, chk.next_version),
        chk.latest_version ? ` (latest ${chk.latest_version}, requires ${chk.required_bump})` : " (first release)"),
      chk.breaking_changes.length ? h("div", {}, h("h3", {}, "Breaking changes"), h("ul", {}, chk.breaking_changes.map((b) => h("li", {}, b)))) : "",
      chk.blockers.length ? h("ul", { class: "blockers" }, chk.blockers.map((b) => h("li", {}, b))) : h("p", { class: "pass" }, "All checks pass."),
      h("h3", {}, "Evals on this hash"),
      h("table", {}, chk.evals.map((e) => h("tr", {}, h("td", {}, e.suite), h("td", {}, e.required ? "required" : "optional"),
        h("td", { class: e.passed ? "pass" : "fail" }, e.run ? `${e.run.passed}/${e.run.total}` : "not run")))),
      h("h3", {}, `Approvals (${chk.approvals.valid.length}/${chk.approvals.required})`),
      h("p", { class: "muted" }, chk.approvals.separation_of_duties ? `Separation of duties: ${chk.approvals.excluded.join(", ") || "—"} cannot approve.` : ""),
      h("table", {}, chk.approvals.all.map((a) => h("tr", {}, h("td", {}, a.approver), h("td", {}, statusBadge(a.decision === "approve" ? "ok" : "failed")), h("td", {}, a.comment), h("td", { class: "muted" }, a.at.slice(0, 19))))),
      h("div", { class: "row" }, field("Comment", comment),
        h("div", { style: "flex:0 0 auto; display:flex; gap:8px" },
          h("button", { onclick: () => act(async () => { await api("POST", `/agents/${agent.name}/approvals`, { decision: "approve", comment: comment.value, content_hash: chk.content_hash }); toast("Approved"); route(); }) }, "Approve"),
          h("button", { class: "danger", onclick: () => act(async () => { await api("POST", `/agents/${agent.name}/approvals`, { decision: "reject", comment: comment.value, content_hash: chk.content_hash }); toast("Rejected"); route(); }) }, "Reject")))),
    h("div", { class: "panel" }, h("h2", {}, "Publish"),
      h("div", { class: "row" }, field("Version bump", bump), field("Changelog", changelog), field("Point alias", alias),
        h("div", { style: "flex:0 0 auto" }, h("button", { class: "primary", disabled: !chk.ok, onclick: () => act(async () => {
          const r = await api("POST", `/agents/${agent.name}/publish`, { bump: bump.value || undefined, changelog: changelog.value, alias: alias.value || undefined });
          toast(`Published ${r.version}`); location.hash = `#/agents/${agent.name}/versions`;
        }) }, "Publish")))));
}

// ------------------------------------------------------------- versions & aliases

async function tabVersions(agent) {
  const versions = agent.versions;
  const vopts = versions.map((v) => [v.version, `${v.version} [${v.status}]`]);
  const aliasBoxes = agent.aliases.map((a) => aliasEditor(agent, a, vopts));
  const newAlias = h("input", { placeholder: "prod" });
  const newVer = select(vopts, versions[0] && versions[0].version, () => {});
  return h("div", {},
    h("div", { class: "panel" }, h("h2", {}, "Aliases"), aliasBoxes.length ? aliasBoxes : h("p", { class: "muted" }, "No aliases."),
      h("h3", {}, "New alias"), h("div", { class: "row" }, field("Alias", newAlias), field("Version", newVer),
        h("div", { style: "flex:0 0 auto" }, h("button", { class: "primary", onclick: () => act(async () => {
          await api("PUT", `/agents/${agent.name}/aliases/${newAlias.value.trim()}`, { version: newVer.value, reason: "created in console" }); route();
        }) }, "Create")))),
    h("div", { class: "panel" }, h("h2", {}, "Versions (immutable)"),
      h("table", {}, h("tr", {}, ["Version", "Status", "Hash", "Published", "Changelog", "Breaking"].map((x) => h("th", {}, x))),
        versions.map((v) => h("tr", {}, h("td", {}, h("a", { href: "javascript:void 0", onclick: () => act(async () => toast(JSON.stringify((await api("GET", `/agents/${agent.name}/versions/${v.version}`)).definition, null, 2).slice(0, 1400))) }, v.version)),
          h("td", {}, statusBadge(v.status), v.flag_reason ? h("div", { class: "muted" }, v.flag_reason) : ""),
          h("td", {}, h("code", {}, v.content_hash.slice(7, 19))), h("td", {}, v.published_by, h("div", { class: "muted" }, v.published_at.slice(0, 19))),
          h("td", {}, v.changelog), h("td", {}, v.breaking.join("; ")))))));
}

function aliasEditor(agent, a, vopts) {
  const ver = select(vopts, a.version, () => {});
  const can = select([["", "— none —"], ...vopts], a.canary_version || "", () => {});
  const w = h("input", { type: "number", min: 0, max: 100, value: a.canary_weight || 0 });
  const reason = h("input", { placeholder: "reason" });
  const hist = h("div", {});
  return h("div", { class: "list-item" },
    h("h3", {}, h("code", {}, a.alias), " → ", a.version, a.canary_version ? ` (canary ${a.canary_version} @ ${a.canary_weight}%)` : ""),
    h("div", { class: "row" }, field("Version", ver), field("Canary version", can), field("Canary %", w), field("Reason", reason),
      h("div", { style: "flex:0 0 auto; display:flex; gap:8px" },
        h("button", { class: "primary", onclick: () => act(async () => {
          await api("PUT", `/agents/${agent.name}/aliases/${a.alias}`, { version: ver.value, canary_version: can.value || null, canary_weight: Number(w.value || 0), reason: reason.value });
          toast(`${a.alias} moved`); route();
        }) }, "Move"),
        h("button", { onclick: () => act(async () => { await api("POST", `/agents/${agent.name}/aliases/${a.alias}/rollback`, { reason: reason.value || "rollback" }); toast("Rolled back"); route(); }) }, "Rollback"),
        h("button", { onclick: () => act(async () => {
          const { history } = await api("GET", `/agents/${agent.name}/aliases/${a.alias}/history`);
          hist.replaceChildren(h("table", {}, history.map((x) => h("tr", {}, h("td", { class: "muted" }, x.at.slice(0, 19)), h("td", {}, `${x.previous_version || "—"} → ${x.version}`),
            h("td", {}, x.canary_version ? `canary ${x.canary_version}@${x.canary_weight}%` : ""), h("td", {}, x.actor), h("td", {}, x.reason)))));
        }) }, "History"))),
    hist);
}

async function tabAgentAudit(agent) {
  const { events } = await api("GET", "/audit?target=" + encodeURIComponent("agent/" + agent.name));
  return h("div", { class: "panel" }, auditTable(events));
}

const auditTable = (events) => h("table", {}, h("tr", {}, ["At", "Actor", "Action", "Target", "Details"].map((x) => h("th", {}, x))),
  events.map((e) => h("tr", {}, h("td", { class: "muted" }, e.at.slice(0, 19)), h("td", {}, e.actor), h("td", {}, h("code", {}, e.action)), h("td", {}, e.target), h("td", {}, h("code", {}, JSON.stringify(e.details).slice(0, 240))))));

// --------------------------------------------------------------------------- catalog

async function viewCatalog() {
  const { models } = await api("GET", "/catalog/models");
  const { tools } = await api("GET", "/catalog/tools");
  const { knowledge } = await api("GET", "/catalog/knowledge");
  const editor = h("div", {});
  const edit = (kind, item) => {
    const id = h("input", { value: item ? item.id : "" });
    const data = Object.assign({}, item || {}); ["id", "updated_by", "updated_at"].forEach((k) => delete data[k]);
    if (kind === "models") delete data.status;
    let doc = data;
    const kindSpecs = kind === "tools" ? state.plugins.tool : kind === "knowledge" ? state.plugins.knowledge : null;
    const cfgBox = h("div", {});
    const renderCfg = () => {
      if (!kindSpecs) return;
      const spec = kindSpecs.find((s) => s.type === doc.kind);
      cfgBox.replaceChildren(spec ? h("fieldset", {}, h("legend", {}, "config (" + spec.title + ")"), schemaForm(spec.params_schema, doc.config, (v) => { doc.config = v || {}; ta.value = JSON.stringify(doc, null, 2); })) : "");
    };
    const ta = jsonArea(doc, (v) => { doc = v || {}; renderCfg(); }, 14);
    renderCfg();
    editor.replaceChildren(h("div", { class: "panel" }, h("h2", {}, (item ? "Edit " : "New ") + kind.replace(/s$/, "")),
      field("id", id), cfgBox, field("Entry (JSON)", ta),
      h("button", { class: "primary", onclick: () => act(async () => { await api("PUT", `/catalog/${kind}/${id.value.trim()}`, doc); toast("Saved"); route(); }) }, "Save")));
    editor.scrollIntoView({ behavior: "smooth" });
  };
  const statusSel = (m) => {
    const s = select(["active", "preview", "deprecated", "retired", "blocked"].map((x) => [x, x]), m.status, (v) => act(async () => {
      if (!confirm(`Set ${m.id} to ${v}? Deprecated/retired/blocked flags every published version using it.`)) { s.value = m.status; return; }
      const r = await api("PATCH", `/catalog/models/${m.id}/status`, { status: v, reason: "console" });
      toast(`${m.id} → ${v}; flagged ${r.flagged_versions.length} version(s)`); route();
    }));
    return s;
  };
  return h("div", {}, h("h1", {}, "Catalog"),
    h("div", { class: "panel" }, h("h2", {}, "Models"),
      h("p", { class: "muted" }, "Vertex models are seeded as preview: verify the model ID, regional availability and pricing before setting them active."),
      h("table", {}, h("tr", {}, ["Id", "Provider / model", "Location", "Status", "Pricing $/Mtok", "Notes", ""].map((x) => h("th", {}, x))),
        models.map((m) => h("tr", {}, h("td", {}, m.id, h("div", {}, h("button", { class: "small-btn", title: "Send a tiny request through the runtime", onclick: () => act(async () => {
            toast(`Testing ${m.id}…`);
            const r = await api("POST", `/catalog/models/${m.id}/test`);
            toast(r.ok ? `✓ ${m.id} answered “${String(r.reply).slice(0, 60)}” in ${r.latency_ms} ms` : `✗ ${m.id}: ${r.error ? r.error.message : "failed"}`, !r.ok);
          }) }, "Test"))), h("td", {}, `${m.provider} / ${m.model_id}`), h("td", {}, m.location), h("td", {}, statusSel(m)),
          h("td", {}, `${m.pricing.input_per_mtok ?? "?"} / ${m.pricing.output_per_mtok ?? "?"}`), h("td", { class: "muted" }, m.notes),
          h("td", {}, h("button", { onclick: () => edit("models", m) }, "Edit"))))),
      h("button", { onclick: () => edit("models", null) }, "+ Model")),
    h("div", { class: "panel" }, h("h2", {}, "Tools"),
      h("table", {}, h("tr", {}, ["Id", "Kind", "Classification", "Owner", "Status", ""].map((x) => h("th", {}, x))),
        tools.map((t) => h("tr", {}, h("td", {}, t.id, h("div", { class: "muted" }, t.description)), h("td", {}, t.kind), h("td", {}, t.data_classification), h("td", {}, t.owner_bu || "platform"), h("td", {}, statusBadge(t.status)),
          h("td", {}, h("button", { onclick: () => edit("tools", t) }, "Edit"))))),
      h("button", { onclick: () => edit("tools", null) }, "+ Tool")),
    h("div", { class: "panel" }, h("h2", {}, "Knowledge sources"),
      h("table", {}, h("tr", {}, ["Id", "Kind", "Classification", "Owner", "Status", ""].map((x) => h("th", {}, x))),
        knowledge.map((k) => h("tr", {}, h("td", {}, k.id, h("div", { class: "muted" }, k.description)), h("td", {}, k.kind), h("td", {}, k.data_classification), h("td", {}, k.owner_bu || "platform"), h("td", {}, statusBadge(k.status)),
          h("td", {}, h("button", { onclick: () => edit("knowledge", k) }, "Edit"))))),
      h("button", { onclick: () => edit("knowledge", null) }, "+ Knowledge source")),
    editor);
}

// -------------------------------------------------------------------------- policies

async function viewPolicies() {
  const platform = await api("GET", "/policies/platform");
  const bu = h("input", { placeholder: "retail" });
  const buBox = h("div", {});
  let platformDoc = platform;
  const loadBu = () => act(async () => {
    const b = bu.value.trim(); if (!b) return;
    const [overlay, eff] = await Promise.all([api("GET", `/policies/bu/${b}`), api("GET", `/policies/effective/${b}`)]);
    let doc = Object.keys(overlay).length ? overlay : { limits: {}, required_guardrails: [], soft_controls: [], release: {} };
    buBox.replaceChildren(h("div", { class: "row" },
      field(`Overlay for ${b} (may only tighten)`, jsonArea(doc, (v) => (doc = v || {}), 22)),
      field("Effective policy", pre(eff))),
      h("button", { class: "primary", onclick: () => act(async () => { await api("PUT", `/policies/bu/${b}`, doc); toast("Overlay saved"); loadBu(); }) }, "Save overlay"));
  });
  return h("div", {}, h("h1", {}, "Policies"),
    h("div", { class: "panel" }, h("h2", {}, "Platform baseline"),
      h("p", { class: "muted" }, "Hard controls (limits, model/tool lists, required guardrails, release rules) are enforced in code; soft_controls are appended to every system prompt."),
      jsonArea(platform, (v) => (platformDoc = v), 24),
      h("button", { class: "primary", onclick: () => act(async () => { await api("PUT", "/policies/platform", platformDoc); toast("Platform policy saved"); }) }, "Save platform policy")),
    h("div", { class: "panel" }, h("h2", {}, "Business-unit overlay"),
      h("div", { class: "row" }, field("BU", bu), h("div", { style: "flex:0 0 auto" }, h("button", { onclick: loadBu }, "Load"))), buBox));
}

// ----------------------------------------------------------------------------- graph

async function viewGraph() {
  const g = await api("GET", "/graph");
  const byFrom = {};
  g.edges.forEach((e) => (byFrom[e.from] = byFrom[e.from] || []).push(e));
  const agents = g.nodes.filter((n) => n.kind === "agent" && n.version);
  return h("div", {}, h("h1", {}, "Dependency graph"),
    h("p", { class: "muted" }, "Live versions (referenced by an alias or canary) and what they depend on. Model status changes flag dependent versions."),
    h("div", { class: "panel" }, agents.length ? h("table", {}, h("tr", {}, ["Agent version", "Aliases", "Depends on"].map((x) => h("th", {}, x))),
      agents.map((n) => h("tr", {}, h("td", {}, h("a", { href: "#/agents/" + n.name + "/versions" }, `${n.name}@${n.version}`)),
        h("td", {}, (n.aliases || []).join(", ")),
        h("td", {}, (byFrom[n.id] || []).map((e) => h("div", {}, badge(e.kind), " ", e.to.replace(/^\w+:/, "")))))))
      : h("p", { class: "muted" }, "Nothing published yet.")));
}

// ----------------------------------------------------------------------------- roles

async function viewRoles() {
  const { bindings } = await api("GET", "/role-bindings");
  const p = h("input", { placeholder: "user@example.com" }), bu = h("input", { placeholder: "retail or *" });
  const role = select(["viewer", "functional_author", "author", "approver", "publisher", "bu_admin", "platform_admin"].map((r) => [r, r]), "viewer", () => {});
  return h("div", {}, h("h1", {}, "Role bindings"),
    h("div", { class: "panel" }, h("table", {}, h("tr", {}, ["Principal", "BU", "Role", "Granted by", ""].map((x) => h("th", {}, x))),
      bindings.map((b) => h("tr", {}, h("td", {}, b.principal), h("td", {}, b.bu), h("td", {}, b.role), h("td", { class: "muted" }, b.granted_by),
        h("td", {}, h("button", { class: "danger", onclick: () => act(async () => { await api("DELETE", "/role-bindings", b); route(); }) }, "Revoke")))))),
    h("div", { class: "panel" }, h("h2", {}, "Grant"), h("div", { class: "row" }, field("Principal", p), field("BU", bu), field("Role", role),
      h("div", { style: "flex:0 0 auto" }, h("button", { class: "primary", onclick: () => act(async () => { await api("POST", "/role-bindings", { principal: p.value.trim(), bu: bu.value.trim(), role: role.value }); route(); }) }, "Grant")))));
}

async function viewAudit() {
  const { events } = await api("GET", "/audit?limit=300");
  return h("div", {}, h("h1", {}, "Audit log"), h("div", { class: "panel" }, auditTable(events)));
}

// ------------------------------------------------------------------------------ boot

async function boot() {
  ready = loadShared().catch((e) => fail(e));
  route();
}
window.addEventListener("hashchange", route);
boot();
