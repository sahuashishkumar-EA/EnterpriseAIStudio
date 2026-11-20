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
    err.details = data.error && data.error.details; err.status = res.status; err.code = data.error && data.error.code;
    throw err;
  }
  return data;
}

function toast(msg, bad) {
  const t = document.getElementById("toast");
  if (msg instanceof Node) t.replaceChildren(msg); else t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  const hide = () => { clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), bad ? 10000 : 3500); };
  t.onmouseenter = () => clearTimeout(toast._t); t.onmouseleave = hide; t.onclick = (e) => { if (e.target === t) t.hidden = true; };
  hide();
}
// Errors in plain language with what to do about them (monitor.js), technical details folded away.
function fail(e) { toast(typeof errorView === "function" ? errorView(e) : e.message, true); }
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
    ...((!state.me || state.settings.dev_auth) ? [
      h("span", { title: "Local development only: sent as X-Dev-* headers." }, "dev:"), user, roles,
      h("button", { onclick: () => { setDevIdentity(user.value ? { user: user.value, roles: roles.value } : null); boot(); } }, "Use")] : []));
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
  [/^#\/monitor(?:\?.*)?$/, () => viewMonitor()],
  [/^#\/roles$/, () => { location.hash = "#/people"; return h("div"); }],
  [/^#\/people(?:\?.*)?$/, () => viewPeople()],
  [/^#\/audit$/, viewAudit],
];

let ready = Promise.resolve();
let currentHash = location.hash;
let skipGuard = false;
async function route() {
  await ready; // never render a screen before identity, settings and catalog are loaded
  // Screens with editors register state.dirty(); leaving them with unsaved edits asks first.
  if (skipGuard) { skipGuard = false; if (location.hash === currentHash) return; }
  if (state.dirty && state.dirty() && location.hash !== currentHash) {
    if (!confirm("You have unsaved changes on this screen. Leave and discard them?")) {
      skipGuard = true; location.hash = currentHash; return;
    }
  }
  state.dirty = null; window.onbeforeunload = null;
  currentHash = location.hash;
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
    if (m) { view.replaceChildren(h("p", { class: "muted" }, "Loading…")); try { view.replaceChildren(await fn(m)); } catch (e) { view.replaceChildren(errorView(e)); } return; }
  }
  location.hash = "#/agents";
}

// ------------------------------------------------------------------------ agents

function buPicker(info) {
  const opts = info.bus.map((b) => [b, b]);
  if (info.can_create) opts.push(["__new__", "+ New business unit…"]);
  const sel = select([["", "— choose —"], ...opts], info.bus.length === 1 ? info.bus[0] : "", () => {});
  const custom = h("input", { placeholder: "new business unit, e.g. retail", style: "display:none; margin-top:6px" });
  sel.addEventListener("change", () => { custom.style.display = sel.value === "__new__" ? "" : "none"; });
  const box = h("div", {}, sel, custom);
  Object.defineProperty(box, "value", { get: () => (sel.value === "__new__" ? custom.value.trim().toLowerCase() : sel.value) });
  return box;
}

async function viewAgents() {
  const [{ agents }, buInfo] = await Promise.all([api("GET", "/agents"), api("GET", "/bus")]);
  const name = h("input", { placeholder: "customer-profile" });
  const bu = buPicker(buInfo);
  const desc = h("input", { placeholder: "What the agent does" });
  const me = state.me.principal;
  const search = h("input", { placeholder: "Search agents…", type: "search" });
  const mine = h("input", { type: "checkbox" });
  const buFilter = select([["", "All business units"], ...buInfo.bus.map((b) => [b, b])], "", () => render());
  const grid = h("div", { class: "agent-grid" });
  const card = (a) => h("a", { class: "agent-card", href: "#/agents/" + a.name },
    h("div", { class: "ac-head" }, h("b", {}, a.name), h("span", { class: "muted small" }, a.owner_bu)),
    h("div", { class: "muted small ac-desc" }, a.description || "No description"),
    h("div", { class: "ac-badges" },
      a.live_version ? badge("live " + a.live_version, "ok") : badge("not live"),
      a.has_changes && a.live_version ? badge("unpublished changes", "warn") : "",
      a.chat_live ? badge("in chat app", "ok") : "",
      a.extends ? badge(`🏢 ${a.owner_bu} variant of ${a.extends.agent}`) : "",
      a.customizable ? badge("BUs can customize") : ""),
    h("div", { class: "muted small" }, a.contexts.length ? `${a.contexts.length} context(s): ${a.contexts.slice(0, 3).join(", ")}` : "no contexts",
      " · ", a.model || "no model"));
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const list = agents.filter((a) => (!q || (a.name + " " + (a.description || "") + " " + a.contexts.join(" ")).toLowerCase().includes(q))
      && (!mine.checked || a.created_by === me || a.editors.includes(me)) && (!buFilter.value || a.owner_bu === buFilter.value));
    grid.replaceChildren(...(list.length ? list.map(card) : [h("p", { class: "muted" }, agents.length ? "No agents match." : "No agents yet — create one below.")]));
  };
  search.addEventListener("input", render); mine.addEventListener("change", render);
  render();
  return h("div", {},
    h("h1", {}, "Agents"),
    h("div", { class: "filters" }, search, buFilter, h("label", { class: "toggle" }, mine, h("span", {}, "Only mine"))),
    grid,
    h("div", { class: "panel" }, h("h2", {}, "New agent"),
      h("div", { class: "row" }, h("div", {}, h("label", {}, "Name"), name), h("div", {}, h("label", {}, "Owner BU"), bu),
        h("div", {}, h("label", {}, "Description"), desc),
        h("div", {}, h("button", { class: "primary", onclick: () => act(async () => {
          await api("POST", "/agents", { name: name.value.trim(), owner_bu: bu.value.trim(), description: desc.value });
          location.hash = "#/agents/" + name.value.trim();
        }) }, "Create")))),
    h("details", { class: "panel" }, h("summary", {}, h("b", {}, "Import an agent from JSON")),
      h("p", { class: "muted" }, "Paste or upload a full definition, including functional contexts — e.g. ",
        h("code", {}, "examples/studio/bank-assistant.json"), ". Name and BU are taken from the JSON."),
      (() => {
        const ta = h("textarea", { rows: 6, placeholder: '{"name": "bank-assistant", "owner_bu": "retail", ...}' });
        const file = h("input", { type: "file", accept: ".json" });
        file.addEventListener("change", async () => { if (file.files[0]) ta.value = await file.files[0].text(); });
        return h("div", {}, field("Upload file", file), field("Definition JSON", ta),
          h("button", { class: "primary", onclick: () => act(async () => {
            let d; try { d = JSON.parse(ta.value); } catch (e) { throw new Error("Not valid JSON: " + e.message); }
            await api("POST", "/agents", { name: d.name, owner_bu: d.owner_bu, description: d.description || "", definition: d });
            toast(`Imported ${d.name}`); location.hash = "#/agents/" + d.name;
          }) }, "Import"));
      })()));
}

function agentTabs(agent) {
  const tabs = [["overview", "Overview"], ["studio", "🧑‍💼 Functional studio"]];
  if (agent && agent.variant_of) tabs.push(["bu", "🏢 BU settings"]);
  else if (persona() === "technical") tabs.push(["technical", "🛠️ Technical setup"]);
  tabs.push(["release", "🚀 Release"], ["audit", "History"]);
  return tabs;
}

async function viewAgent(name, tab) {
  const agent = await api("GET", "/agents/" + encodeURIComponent(name));
  window.onbeforeunload = null;
  if (["evals", "versions"].includes(tab)) tab = "release"; // old links
  if (agent.variant_of && ["technical", "definition"].includes(tab)) tab = "bu";
  const body = await ({ overview: tabOverview, studio: tabStudio, technical: tabDefinition, definition: tabDefinition,
    bu: tabBuSettings, release: tabReleaseFlow, audit: tabAgentAudit }[tab] || tabOverview)(agent);
  const tabs = agentTabs(agent);
  if (tab === "technical" && !tabs.some(([k]) => k === "technical")) tabs.splice(2, 0, ["technical", "🛠️ Technical setup"]);
  return h("div", {},
    h("div", { class: "crumbs" }, h("a", { href: "#/agents" }, "Agents"), " / ", agent.owner_bu),
    h("h1", {}, agent.name, agent.aliases.some((a) => a.alias === "prod") ? h("span", { class: "badge ok", style: "margin-left:10px" }, "live") : h("span", { class: "badge", style: "margin-left:10px" }, "not live"),
      hostingBadge(agent.draft.definition),
      agent.variant_of ? h("span", { class: "badge", style: "margin-left:8px" }, `variant of ${agent.variant_of}`) : ""),
    variantBanner(agent),
    h("div", { class: "tabs" }, tabs.map(([k, label]) => h("a", { href: `#/agents/${name}/${k}`, class: k === tab ? "active" : "" }, label))),
    body);
}

// --------------------------------------------------------------- definition editor

function tabDefinition(agent) {
  const def = structuredClone(agent.draft.definition);
  let base = agent.draft.sections.technical;
  let techDirty = false;
  state.dirty = () => techDirty;
  window.onbeforeunload = () => (techDirty ? "You have unsaved changes" : undefined);
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
  const formEditor = () => h("div", {}, sectionBasics(def), sectionHosting(def), sectionCustomization(def), sectionPrompt(def), sectionModel(def), sectionPattern(def),
    sectionBindings(def), sectionGuardrails(def), sectionLimits(def), sectionOutput(def), sectionContract(def));

  const save = () => act(async () => {
    let d;
    try {
      d = await api("PUT", `/agents/${agent.name}/draft/technical`, { definition: def, base });
    } catch (e) {
      if (e.status === 409) { toast("Someone else changed the technical setup since you opened it. Reload to see their changes.", true); return; }
      throw e;
    }
    base = d.sections.technical; techDirty = false;
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
  for (const ev of ["input", "change"]) container.addEventListener(ev, () => { techDirty = true; });
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

// ------------------------------------------------------------- hosting (external agents)

const PLATFORMS = { internal: "This platform", copilot_studio: "Microsoft Copilot Studio", http: "Other platform (HTTP)", a2a: "A2A agent (preview)" };
function hostingBadge(def) {
  const p = (def.hosting || {}).platform || "internal";
  return p === "internal" ? "" : h("span", { class: "badge", style: "margin-left:8px", title: "Built and run outside the platform; governed here." }, "runs on " + PLATFORMS[p]);
}

function sectionHosting(def) {
  def.hosting = def.hosting || { platform: "internal" };
  const hs = def.hosting;
  const body = h("div", {});
  const csv = (key, placeholder) => { const i = h("input", { placeholder, value: (hs[key] || []).join(", ") }); i.onchange = () => (hs[key] = i.value.split(",").map((s) => s.trim()).filter(Boolean)); return i; };
  const render = () => {
    const p = hs.platform || "internal";
    if (p === "internal") {
      body.replaceChildren(h("p", { class: "muted" }, "The platform runtime executes this agent with the model, orchestration and tools below."));
      return;
    }
    const edge = (state.settings.edge_url || "https://<edge-url>").replace(/\/$/, "");
    const ep = (path) => h("code", {}, `${edge}/gov/v1/agents/${def.name}/${path}`);
    body.replaceChildren(
      h("p", { class: "muted" }, "The agent is built and run on ", PLATFORMS[p], ". The governor still owns its behaviour: ",
        "the published instructions and guardrails, tests before every go-live, monitoring and audit. The model, orchestration and tools below are used only for the live preview here."),
      h("div", { class: "row" },
        field(p === "copilot_studio" ? "Direct Line URL (empty = Microsoft's public endpoint)" : "Agent endpoint (tests send it each case)",
          bind(hs, "endpoint", h("input", { placeholder: p === "copilot_studio" ? "https://directline.botframework.com/v3/directline" : "https://agent.example.com/chat" }))),
        field(p === "copilot_studio" ? "Secret name holding the Direct Line secret" : "Secret name holding the bearer token",
          bind(hs, "auth_secret", h("input", { placeholder: p === "copilot_studio" ? "DIRECTLINE_HR_COPILOT" : "HR_AGENT_TOKEN" })))),
      h("div", { class: "row" },
        field("Allowed callers of the governance API (service names, Entra app ids, user:<email>)", csv("callers", "e.g. 2f6c…-app-id, copilot-connector")),
        field("Change webhook (called when a new version goes live)", bind(hs, "webhook_url", h("input", { placeholder: "https://prod-00.westeurope.logic.azure.com/…" }))),
        field("Secret name for signing the webhook", bind(hs, "webhook_secret", h("input", { placeholder: "HR_COPILOT_WEBHOOK_SECRET" })))),
      h("p", { class: "muted small" }, "Secrets are referenced by name only. Store the value in Secret Manager and mount it on the governor as an environment variable of that name."),
      h("details", { class: "howto", open: true }, h("summary", {}, "How the external agent connects"),
        h("ol", {},
          h("li", {}, "At start-up, and whenever the webhook fires, read the governed instructions: GET ", ep("profile?context=<context id>"),
            ". Use ", h("code", {}, "instructions"), " as the agent's instructions (system prompt). The response has an ETag, so poll with If-None-Match."),
          h("li", {}, "Before answering each message: POST ", ep("check/input"), " with ", h("code", {}, '{"message","context","user"}'),
            ". If ", h("code", {}, "decision"), " is respond or block, send ", h("code", {}, "message"), " to the user and stop. Otherwise continue with ", h("code", {}, "text"), " (personal data redacted)."),
          h("li", {}, "Before showing the answer: POST ", ep("check/output"), " with ", h("code", {}, '{"check_id","output","context"}'), " and show the returned ", h("code", {}, "output"), "."),
          h("li", {}, "If the platform cannot call the checks, report each finished turn: POST ", ep("runs"), ". Monitor and the dashboard then cover it."),
          p === "copilot_studio" ? h("li", {}, "Copilot Studio: call these from a custom connector or a Power Automate flow (topic “Conversation Start” for the profile, “On message received” for check/input). Authenticate with an Entra ID app (client credentials). Its app id goes into Allowed callers. The tests in Release use the Direct Line secret from Settings → Security → Web channel security.") : "",
          h("li", {}, "Full guide: docs/external-agents.md in the repository."))));
  };
  render();
  return h("div", { class: "panel" }, h("h2", {}, "Where this agent runs"),
    field("Platform", select(Object.entries(PLATFORMS), hs.platform || "internal", (v) => { hs.platform = v; render(); })), body);
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

// Simple field editor for flat object schemas; anything more complex falls back to JSON.
function schemaBuilder(schema, onChange) {
  const TYPES = [["string", "text"], ["number", "number"], ["integer", "whole number"], ["boolean", "yes/no"], ["array", "list of text"]];
  const simple = (sc) => !sc || ((sc.type === "object" || !sc.type) && Object.values(sc.properties || {}).every((p) =>
    ["string", "number", "integer", "boolean"].includes(p.type) || (p.type === "array" && (!p.items || p.items.type === "string"))));
  const box = h("div", { class: "schema-builder" });
  let jsonMode = !simple(schema);
  const rows = Object.entries((schema && schema.properties) || {}).map(([name, p]) => ({
    name, type: p.type || "string", description: p.description || "", required: ((schema && schema.required) || []).includes(name) }));
  const emit = () => {
    const props = {}; const req = [];
    for (const r of rows) {
      if (!r.name.trim()) continue;
      props[r.name.trim()] = r.type === "array" ? { type: "array", items: { type: "string" } } : { type: r.type };
      if (r.description) props[r.name.trim()].description = r.description;
      if (r.required) req.push(r.name.trim());
    }
    onChange(rows.length ? { type: "object", properties: props, required: req } : {});
  };
  const render = () => {
    if (jsonMode) {
      box.replaceChildren(jsonArea(schema, (v) => { schema = v || {}; onChange(schema); }, 8),
        simple(schema) ? h("button", { class: "small-btn", onclick: () => { jsonMode = false; render(); } }, "Edit as fields") :
          h("p", { class: "muted small" }, "This schema is nested, so it is edited as JSON."));
      return;
    }
    box.replaceChildren(
      h("table", { class: "sb-table" }, h("tr", {}, ["Field", "Type", "Required", "Description", ""].map((x) => h("th", {}, x))),
        rows.map((r, i) => h("tr", {},
          h("td", {}, (() => { const x = h("input", { value: r.name, placeholder: "field_name" }); x.oninput = () => { r.name = x.value; emit(); }; return x; })()),
          h("td", {}, select(TYPES, r.type, (v) => { r.type = v; emit(); })),
          h("td", {}, (() => { const x = h("input", { type: "checkbox", checked: r.required }); x.onchange = () => { r.required = x.checked; emit(); }; return x; })()),
          h("td", {}, (() => { const x = h("input", { value: r.description }); x.oninput = () => { r.description = x.value; emit(); }; return x; })()),
          h("td", {}, h("button", { class: "small-btn danger", onclick: () => { rows.splice(i, 1); emit(); render(); } }, "✕"))))),
      h("div", { class: "row-btns" }, h("button", { class: "small-btn", onclick: () => { rows.push({ name: "", type: "string", description: "", required: false }); render(); } }, "+ Field"),
        h("button", { class: "small-btn", onclick: () => { jsonMode = true; render(); } }, "Edit as JSON")));
  };
  render();
  return box;
}

function sectionContract(def) {
  def.contract = def.contract || {};
  return h("div", { class: "panel" }, h("h2", {}, "Interface (contract) ", h("span", { class: "muted" }, "(what callers send and get back; breaking changes force a major version)")),
    field("Description", bind(def.contract, "description", h("input", {}))),
    h("div", { class: "row" },
      field("Input fields", schemaBuilder(def.contract.input_schema, (v) => (def.contract.input_schema = v || {}))),
      field("Output fields", schemaBuilder(def.contract.output_schema, (v) => (def.contract.output_schema = v || {})))));
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
  const alias = h("input", { value: "prod", placeholder: "leave empty to publish without going live" });
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
      h("div", { class: "row" }, field("Version bump", bump), field("Changelog", changelog), field("Go live as (alias)", alias),
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
  const [{ models }, { tools }, { knowledge }, buInfo] = await Promise.all(["models", "tools", "knowledge"].map((k) => api("GET", "/catalog/" + k)).concat([api("GET", "/bus")]));
  const editor = h("div", {});
  const CLASSES = ["public", "internal", "confidential", "restricted"].map((x) => [x, x]);
  const inputFor = (obj, key, attrs, cast) => { const x = h("input", Object.assign({ value: obj[key] ?? "" }, attrs || {})); x.oninput = () => { obj[key] = cast ? cast(x.value) : x.value; }; return x; };
  const edit = (kind, item) => {
    const doc = structuredClone(item || {}); ["updated_by", "updated_at"].forEach((k) => delete doc[k]);
    const id = h("input", { value: doc.id || "", disabled: !!item, placeholder: "unique-id" }); delete doc.id;
    let fields;
    if (kind === "models") {
      delete doc.status; doc.pricing = doc.pricing || {}; doc.params = doc.params || {};
      fields = h("div", {},
        h("div", { class: "row" },
          field("Provider", select(state.plugins.model_provider.map((p) => [p.type, p.title]), doc.provider || "google_ai", (v) => { doc.provider = v; })),
          field("Model name at the provider", inputFor(doc, "model_id", { placeholder: "e.g. gemini-2.5-flash" })),
          field("Location", inputFor(doc, "location", { placeholder: "europe-west4, global, google-ai…" }))),
        h("div", { class: "row" },
          field("Input price ($ per million tokens)", inputFor(doc.pricing, "input_per_mtok", { type: "number", step: "any" }, Number)),
          field("Output price ($ per million tokens)", inputFor(doc.pricing, "output_per_mtok", { type: "number", step: "any" }, Number))),
        field("Notes", inputFor(doc, "notes")),
        h("details", {}, h("summary", {}, "Advanced: provider parameters (JSON)"), jsonArea(doc.params, (v) => (doc.params = v || {}), 4)));
      if (!item) doc.provider = doc.provider || "google_ai";
    } else {
      const specs = kind === "tools" ? state.plugins.tool : state.plugins.knowledge;
      doc.kind = doc.kind || specs[0].type; doc.config = doc.config || {}; doc.status = doc.status || "active";
      const cfgBox = h("div", {});
      const renderCfg = () => { const sp = specs.find((x) => x.type === doc.kind); cfgBox.replaceChildren(sp ? h("fieldset", {}, h("legend", {}, sp.title + " settings"), schemaForm(sp.params_schema, doc.config, (v) => { doc.config = v || {}; })) : ""); };
      renderCfg();
      fields = h("div", {},
        h("div", { class: "row" }, field("Title", inputFor(doc, "title")), field("Type", select(specs.map((x) => [x.type, x.title]), doc.kind, (v) => { doc.kind = v; doc.config = {}; renderCfg(); }))),
        field("Description (the model reads this to decide when to use it)", inputFor(doc, "description")),
        h("div", { class: "row" }, field("Data classification", select(CLASSES, doc.data_classification || "internal", (v) => { doc.data_classification = v; })),
          field("Owner BU", select([["", "platform"], ...buInfo.bus.map((b) => [b, b])], doc.owner_bu || "", (v) => { doc.owner_bu = v || undefined; })),
          field("Status", select(["active", "preview", "deprecated", "retired", "blocked"].map((x) => [x, x]), doc.status, (v) => { doc.status = v; }))),
        cfgBox,
        kind === "tools" ? field("Input fields the model must provide", schemaBuilder(doc.input_schema || {}, (v) => { doc.input_schema = v || {}; })) : "");
    }
    editor.replaceChildren(h("div", { class: "panel" }, h("h2", {}, (item ? "Edit " : "New ") + ({ models: "model", tools: "tool", knowledge: "knowledge source" })[kind]),
      field("Id", id), fields,
      h("div", { class: "row-btns" }, h("button", { onclick: () => editor.replaceChildren() }, "Cancel"),
        h("button", { class: "primary", onclick: () => act(async () => {
          if (!id.value.trim()) throw new Error("Id is required");
          await api("PUT", `/catalog/${kind}/${id.value.trim()}`, doc); toast("Saved"); route();
        }) }, "Save"))));
    editor.scrollIntoView({ behavior: "smooth" });
  };
  const changeStatus = (m, sel) => act(async () => {
    const v = sel.value;
    const { live } = await api("GET", `/catalog/models/${m.id}/usage`);
    const bad = ["deprecated", "retired", "blocked"].includes(v);
    const close = modal(`Set ${m.id} to “${v}”?`,
      live.length ? h("div", {}, h("p", {}, `${live.length} live agent version(s) use this model:`),
        h("ul", {}, live.map((x) => h("li", {}, `${x.agent_name} ${x.version} (${x.alias})`))),
        bad ? h("p", { class: "warn-text" }, v === "deprecated" ? "They keep working but are flagged for review." : "They will stop using this model and fall back to their fallback models — or fail if they have none.") : "")
        : h("p", { class: "muted" }, "No live agent uses this model."),
      h("div", { class: "row-btns", style: "justify-content:flex-end" },
        h("button", { onclick: () => { sel.value = m.status; close(); } }, "Cancel"),
        h("button", { class: bad ? "danger" : "primary", onclick: () => act(async () => {
          const r = await api("PATCH", `/catalog/models/${m.id}/status`, { status: v, reason: "console" });
          close(); toast(`${m.id} → ${v}; flagged ${r.flagged_versions.length} version(s)`); route();
        }) }, `Set to ${v}`)));
  });
  const statusSel = (m) => { const sel = select(["active", "preview", "deprecated", "retired", "blocked"].map((x) => [x, x]), m.status, () => changeStatus(m, sel)); return sel; };
  const testBtn = (m) => h("button", { class: "small-btn", title: "Send a tiny request through the runtime", onclick: () => act(async () => {
    toast(`Testing ${m.id}…`);
    const r = await api("POST", `/catalog/models/${m.id}/test`);
    toast(r.ok ? `✓ ${m.id} answered “${String(r.reply).slice(0, 60)}” in ${r.latency_ms} ms` : `✗ ${m.id}: ${r.error ? r.error.message : "failed"}`, !r.ok);
  }) }, "Test");
  return h("div", {}, h("h1", {}, "Catalog"),
    h("div", { class: "panel" }, h("h2", {}, "Models"),
      h("p", { class: "muted" }, "Test a model before setting it active. Only active models can be used by published agents."),
      h("table", {}, h("tr", {}, ["Model", "Provider / name", "Location", "Status", "$ in / out per M tokens", ""].map((x) => h("th", {}, x))),
        models.map((m) => h("tr", {}, h("td", {}, h("b", {}, m.id), h("div", { class: "muted small" }, m.notes)), h("td", {}, `${m.provider} / ${m.model_id}`), h("td", {}, m.location),
          h("td", {}, statusSel(m)), h("td", {}, `${m.pricing.input_per_mtok ?? "?"} / ${m.pricing.output_per_mtok ?? "?"}`),
          h("td", { class: "row-btns" }, testBtn(m), h("button", { class: "small-btn", onclick: () => edit("models", m) }, "Edit"))))),
      h("button", { onclick: () => edit("models", null) }, "+ Model")),
    h("div", { class: "panel" }, h("h2", {}, "Tools"),
      h("table", {}, h("tr", {}, ["Tool", "Type", "Classification", "Owner", "Status", ""].map((x) => h("th", {}, x))),
        tools.map((t) => h("tr", {}, h("td", {}, h("b", {}, t.id), h("div", { class: "muted small" }, t.description)), h("td", {}, t.kind), h("td", {}, t.data_classification), h("td", {}, t.owner_bu || "platform"), h("td", {}, statusBadge(t.status)),
          h("td", {}, h("button", { class: "small-btn", onclick: () => edit("tools", t) }, "Edit"))))),
      h("button", { onclick: () => edit("tools", null) }, "+ Tool")),
    h("div", { class: "panel" }, h("h2", {}, "Knowledge sources"),
      h("table", {}, h("tr", {}, ["Source", "Type", "Classification", "Owner", "Status", ""].map((x) => h("th", {}, x))),
        knowledge.map((k) => h("tr", {}, h("td", {}, h("b", {}, k.title || k.id), h("div", { class: "muted small" }, k.id)), h("td", {}, k.kind), h("td", {}, k.data_classification), h("td", {}, k.owner_bu || "platform"), h("td", {}, statusBadge(k.status)),
          h("td", {}, h("button", { class: "small-btn", onclick: () => edit("knowledge", k) }, "Edit"))))),
      h("button", { onclick: () => edit("knowledge", null) }, "+ Knowledge source")),
    editor);
}

// -------------------------------------------------------------------------- policies

const LIMIT_LABELS = { max_cost_usd: "Max cost per run ($)", max_input_tokens: "Max input tokens", max_output_tokens: "Max output tokens",
  max_latency_ms: "Max response time (ms)", max_tool_calls: "Max tool calls", max_iterations: "Max reasoning steps", max_depth: "Max agent-call depth" };

// Form for a platform policy or a BU overlay. For overlays, `platform` gives the ceiling and the form only allows tightening.
function policyForm(doc, platform) {
  const overlay = !!platform;
  doc.limits = doc.limits || {}; doc.release = doc.release || {}; doc.soft_controls = doc.soft_controls || [];
  doc.denied_models = doc.denied_models || []; doc.denied_tools = doc.denied_tools || [];
  const errors = h("div", { class: "fail small" });
  const check = () => {
    if (!overlay) return true;
    const errs = [];
    for (const [k, v] of Object.entries(doc.limits)) if (platform.limits[k] != null && v > platform.limits[k]) errs.push(`${LIMIT_LABELS[k] || k}: ${v} is above the platform's ${platform.limits[k]}`);
    if (doc.release.min_approvals != null && doc.release.min_approvals < platform.release.min_approvals) errs.push("Approvals cannot be lower than the platform's");
    if (doc.release.min_eval_pass_rate != null && doc.release.min_eval_pass_rate < platform.release.min_eval_pass_rate) errs.push("Test pass rate cannot be lower than the platform's");
    if (doc.daily_budget_usd != null && platform.daily_budget_usd != null && doc.daily_budget_usd > platform.daily_budget_usd) errs.push("Daily budget cannot exceed the platform's");
    errors.replaceChildren(...errs.map((e) => h("div", {}, "✗ " + e)));
    return !errs.length;
  };
  const num = (obj, key, ceiling) => {
    const x = h("input", { type: "number", step: "any", value: obj[key] ?? "", placeholder: overlay ? `inherit (${ceiling ?? "none"})` : "" });
    x.oninput = () => { if (x.value === "") delete obj[key]; else obj[key] = Number(x.value); check(); };
    return x;
  };
  const tri = (obj, key) => select(overlay ? [["", "inherit"], ["true", "yes"], ["false", "no"]] : [["true", "yes"], ["false", "no"]],
    obj[key] == null ? "" : String(obj[key]), (v) => { if (v === "") delete obj[key]; else obj[key] = v === "true"; check(); });
  const form = h("div", { class: "policy-form" },
    h("h3", {}, "Limits per run"), h("div", { class: "grid3" }, Object.keys(LIMIT_LABELS).map((k) =>
      field(LIMIT_LABELS[k] + (overlay ? ` · platform: ${platform.limits[k] ?? "—"}` : ""), num(doc.limits, k, overlay ? platform.limits[k] : null)))),
    h("h3", {}, "Release rules"), h("div", { class: "grid3" },
      field("Approvals required" + (overlay ? ` · platform: ${platform.release.min_approvals}` : ""), num(doc.release, "min_approvals", overlay ? platform.release.min_approvals : null)),
      field("Approver must not be an editor", tri(doc.release, "separation_of_duties")),
      field("Tests must pass before release", tri(doc.release, "require_eval")),
      field("Minimum test pass rate (0–1)" + (overlay ? ` · platform: ${platform.release.min_eval_pass_rate}` : ""), num(doc.release, "min_eval_pass_rate", overlay ? platform.release.min_eval_pass_rate : null)),
      field("Daily budget per BU ($)" + (overlay ? ` · platform: ${platform.daily_budget_usd ?? "none"}` : ""), num(doc, "daily_budget_usd", overlay ? platform.daily_budget_usd : null))),
    h("h3", {}, "Rules added to every agent's instructions (soft controls)"), chips(doc.soft_controls, "e.g. Never promise outcomes", () => {}),
    h("h3", {}, "Blocked models and tools"), h("div", { class: "grid2" },
      field("Models", chips(doc.denied_models, "catalog id", () => {})), field("Tools", chips(doc.denied_tools, "catalog id", () => {}))),
    h("details", {}, h("summary", {}, "Advanced: required guardrails and full JSON"), pre(doc)),
    errors);
  form.valid = check;
  return form;
}

async function viewPolicies() {
  const [platform, buInfo] = await Promise.all([api("GET", "/policies/platform"), api("GET", "/bus")]);
  const buBox = h("div", {});
  const buSel = select([["", "— choose a business unit —"], ...buInfo.bus.map((b) => [b, b])], "", (b) => loadBu(b));
  const loadBu = (b) => act(async () => {
    if (!b) { buBox.replaceChildren(); return; }
    const [overlay, eff] = await Promise.all([api("GET", `/policies/bu/${b}`), api("GET", `/policies/effective/${b}`)]);
    const doc = Object.keys(overlay).length ? overlay : {};
    const f = policyForm(doc, platform);
    buBox.replaceChildren(h("p", { class: "muted" }, `A business unit can only make the platform policy stricter. Leave a field empty to inherit.`), f,
      h("details", {}, h("summary", {}, "Effective policy for " + b), pre(eff)),
      h("button", { class: "primary", onclick: () => act(async () => {
        if (!f.valid()) throw new Error("Fix the highlighted values first: a BU may only tighten the platform policy.");
        await api("PUT", `/policies/bu/${b}`, doc); toast(`Policy for ${b} saved`); loadBu(b);
      }) }, `Save policy for ${b}`));
  });
  const platformDoc = structuredClone(platform);
  const pform = policyForm(platformDoc, null);
  return h("div", {}, h("h1", {}, "Policies"),
    h("div", { class: "panel" }, h("h2", {}, "Platform baseline"),
      h("p", { class: "muted" }, "Applies to every agent. Limits and release rules are enforced in code; the rules below are added to every agent's instructions."),
      pform,
      h("button", { class: "primary", onclick: () => act(async () => { await api("PUT", "/policies/platform", platformDoc); toast("Platform policy saved"); route(); }) }, "Save platform policy")),
    h("div", { class: "panel" }, h("h2", {}, "Business-unit policy"), field("Business unit", buSel), buBox));
}

// ----------------------------------------------------------------------------- graph

async function viewGraph() {
  const g = await api("GET", "/graph");
  if (!g.nodes.length) return h("div", {}, h("h1", {}, "Dependency map"), h("p", { class: "muted" }, "Nothing is live yet."));
  // layer agents by call depth; tools/knowledge/models go to the right
  const agents = g.nodes.filter((n) => n.kind === "agent");
  const layer = {}; agents.forEach((n) => (layer[n.id] = 0));
  for (let i = 0; i < agents.length; i++) for (const e of g.edges) if (e.kind === "agent" && layer[e.from] != null && layer[e.to] != null) layer[e.to] = Math.max(layer[e.to], layer[e.from] + 1);
  const maxA = Math.max(0, ...Object.values(layer));
  const order = ["tool", "knowledge", "model"];
  g.nodes.filter((n) => n.kind !== "agent").forEach((n) => (layer[n.id] = maxA + 1));
  const cols = {}; g.nodes.forEach((n) => (cols[layer[n.id]] ||= []).push(n));
  Object.values(cols).forEach((c) => c.sort((a, b) => (order.indexOf(a.kind) - order.indexOf(b.kind)) || a.id.localeCompare(b.id)));
  const W = 230, H = 44, GX = 90, GY = 16, pos = {};
  const ncol = Object.keys(cols).length;
  const height = Math.max(...Object.values(cols).map((c) => c.length)) * (H + GY) + 20;
  Object.entries(cols).forEach(([ci, c]) => c.forEach((n, i) => (pos[n.id] = { x: 10 + ci * (W + GX), y: 10 + i * (H + GY) })));
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text) e.textContent = text; return e; };
  const svg = el("svg", { width: ncol * (W + GX), height, class: "graph-svg", role: "img", "aria-label": "Dependency map" });
  for (const e of g.edges) {
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) continue;
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
    svg.append(el("path", { d: `M${x1},${y1} C${x1 + 45},${y1} ${x2 - 45},${y2} ${x2},${y2}`, class: "g-edge g-" + e.kind }));
  }
  const ICON = { agent: "🤖", tool: "🔧", knowledge: "📚", model: "🧠" };
  for (const n of g.nodes) {
    const p = pos[n.id]; const grp = el("g", { class: "g-node g-" + n.kind });
    grp.append(el("rect", { x: p.x, y: p.y, width: W, height: H, rx: 8 }));
    grp.append(el("text", { x: p.x + 10, y: p.y + 18 }, `${ICON[n.kind] || ""} ${n.name}`));
    grp.append(el("text", { x: p.x + 10, y: p.y + 34, class: "g-sub" }, n.kind === "agent" ? `${n.version || ""}${(n.aliases || []).length ? " · " + n.aliases.join(", ") : ""}` : n.kind));
    if (n.kind === "agent") grp.addEventListener("click", () => (location.hash = "#/agents/" + n.name + "/release"));
    svg.append(grp);
  }
  return h("div", {}, h("h1", {}, "Dependency map"),
    h("p", { class: "muted" }, "Live agent versions (left) and the agents, tools, knowledge and models they use. Click an agent to open its release page."),
    h("div", { class: "panel graph-wrap" }, svg));
}

// ----------------------------------------------------------------------------- roles

async function viewRoles() {
  const { bindings } = await api("GET", "/role-bindings");
  const p = h("input", { placeholder: "user@example.com" }), bu = h("input", { placeholder: "retail or *" });
  const role = select(["member", "viewer", "functional_author", "author", "approver", "publisher", "bu_admin", "platform_admin"].map((r) => [r, r]), "viewer", () => {});
  return h("div", {},
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
