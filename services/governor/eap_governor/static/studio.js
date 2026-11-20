// Functional studio + Overview. Business-friendly editing of an agent's functional contexts,
// with a live preview chat against the (even unsaved) draft and a "what the model sees" view.
"use strict";

// ------------------------------------------------------------------ orchestration timeline

const STEP_ICON = { guardrail: "🛡️", retrieval: "📚", model: "🧠", tool: "🔧", agent: "🤖", info: "ℹ️" };

function stepLine(st) {
  const d = st.detail || {};
  let what = "";
  if (st.kind === "model") {
    what = `${st.name} · ${d.input_tokens || 0}→${d.output_tokens || 0} tokens · $${(d.cost_usd || 0).toFixed(5)}`;
    if (d.tool_calls && d.tool_calls.length) what += ` · decided to call: ${d.tool_calls.join(", ")}`;
    else if (d.stop_reason) what += ` · ${d.stop_reason === "end" ? "final answer" : d.stop_reason}`;
    if (d.error) what += ` · ${d.error}`;
  } else if (st.kind === "tool") {
    what = `${st.name}(${JSON.stringify(d.arguments || {}).slice(0, 120)}) → ${String(d.result_preview || "").slice(0, 140)}`;
  } else if (st.kind === "retrieval") {
    what = `${st.name}: ${d.hits ?? 0} relevant passage(s)` + (d.query ? ` for “${String(d.query).slice(0, 60)}”` : "");
  } else if (st.kind === "agent") {
    what = `called agent ${st.name} (depth ${d.depth}) · $${(d.cost_usd || 0).toFixed(5)}`;
  } else if (st.kind === "guardrail") {
    what = `${st.name} ${st.status}` + (d.stage ? ` at ${d.stage}` : "") + (d.detail ? ` — ${d.detail}` : "");
  } else {
    what = `${st.name} ${JSON.stringify(d).slice(0, 120)}`;
  }
  return { icon: STEP_ICON[st.kind] || "•", kind: st.kind, status: st.status, what, ms: st.latency_ms || 0 };
}

// "How this answer was made": the orchestration of one run, step by step.
function timelineView(r) {
  const steps = ((r.trace && r.trace.steps) || []).map(stepLine);
  const models = steps.filter((x) => x.kind === "model").length, tools = steps.filter((x) => x.kind === "tool").length;
  const summary = `${steps.length} step(s) · ${models} model call(s) · ${tools} tool call(s) · ${r.latency_ms} ms · $${((r.cost && r.cost.usd) || 0).toFixed(5)}`;
  return h("details", { class: "timeline" }, h("summary", {}, "How this answer was made ", h("span", { class: "muted" }, summary)),
    h("ol", {}, steps.map((x) => h("li", { class: "tl-" + x.status }, h("span", { class: "tl-icon" }, x.icon),
      h("span", { class: "tl-what" }, x.what), h("span", { class: "tl-ms muted" }, x.ms ? `${x.ms} ms` : "")))));
}

// ------------------------------------------------------------------ small widgets

// Safe minimal markdown for preview bubbles: escape, then **bold**, _italic_, `code`, lists, paragraphs.
function mdLite(text) {
  const esc = (x) => x.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inline = (x) => esc(x).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_(.+?)_(?=[\s).,!?]|$)/g, "$1<em>$2</em>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = []; let list = null;
  for (const line of String(text).split("\n")) {
    const m = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);
    if (m) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = null; }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  const d = document.createElement("div"); d.className = "md"; d.innerHTML = out.join(""); return d;
}

// Chip list: type and press Enter to add; × to remove.
function chips(list, placeholder, onChange) {
  const box = h("div", { class: "chips" });
  const input = h("input", { placeholder: placeholder || "Type and press Enter" });
  const render = () => {
    box.replaceChildren(...list.map((v, i) => h("span", { class: "chip" }, v,
      h("button", { class: "chip-x", title: "Remove", onclick: () => { list.splice(i, 1); onChange(); render(); } }, "×"))), input);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || (e.key === "," && input.value.trim())) {
      e.preventDefault();
      const v = input.value.trim().replace(/,$/, "");
      if (v && !list.includes(v)) { list.push(v); onChange(); }
      input.value = ""; render(); input.focus();
    } else if (e.key === "Backspace" && !input.value && list.length) { list.pop(); onChange(); render(); input.focus(); }
  });
  input.addEventListener("blur", () => { const v = input.value.trim(); if (v && !list.includes(v)) { list.push(v); onChange(); input.value = ""; render(); } });
  render();
  return box;
}

// Segmented single choice: options = [[value, label, hint?]]
function segmented(options, value, onChange) {
  const box = h("div", { class: "seg" });
  const render = (cur) => box.replaceChildren(...options.map(([v, label, hint]) =>
    h("button", { class: "seg-opt" + (v === cur ? " on" : ""), title: hint || "", onclick: () => { onChange(v); render(v); } }, label)));
  render(value);
  return box;
}

function toggle(value, label, onChange) {
  const cb = h("input", { type: "checkbox", checked: !!value });
  cb.addEventListener("change", () => onChange(cb.checked));
  return h("label", { class: "toggle" }, cb, h("span", {}, label));
}

function textInput(obj, key, placeholder, onChange, long) {
  const el = long ? h("textarea", { class: "prose", rows: 3, placeholder }) : h("input", { placeholder });
  el.value = obj[key] || "";
  el.addEventListener("input", () => { obj[key] = el.value; onChange(); });
  return el;
}

// Simple modal dialog. Returns a close() function.
function modal(title, ...body) {
  const close = () => overlay.remove();
  const overlay = h("div", { class: "modal-overlay", onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "modal", role: "dialog", "aria-label": title },
      h("div", { class: "modal-head" }, h("h2", {}, title), h("button", { class: "small-btn", onclick: close, "aria-label": "Close" }, "✕")),
      ...body));
  document.body.append(overlay);
  const first = overlay.querySelector("input, textarea, button.primary"); if (first) first.focus();
  return close;
}

const card = (icon, title, hint, ...body) => h("section", { class: "s-card" },
  h("div", { class: "s-card-head" }, h("span", { class: "s-icon", "aria-hidden": "true" }, icon), h("div", {}, h("h3", {}, title), hint ? h("p", { class: "muted" }, hint) : "")),
  ...body);

const TEMPLATES = {
  blank: { name: "New context" },
  support: {
    name: "Customer support", audience: "customers contacting us via the website",
    role: "a friendly customer-support assistant", goal: "Resolve questions quickly and accurately",
    behavior: { tone: "friendly", verbosity: "brief", format: "steps", language: "match the user", emoji: false },
    always: ["Confirm you understood the question", "Offer a next step"], never: ["Make promises about refunds", "Guess when unsure"],
    guardrails: { blocked_topics: ["politics", "medical advice"], refusal_message: "Sorry, I can only help with questions about our products and services.",
      escalation: { triggers: ["complaint", "speak to a human", "cancel my account"], message: "I'll connect you with a colleague who can help." },
      personal_data: "redact", disclaimer: "" },
    starters: ["How do I reset my password?", "Where is my order?"],
  },
  sales: {
    name: "Sales assistant", audience: "prospective customers", role: "a knowledgeable product advisor",
    goal: "Help prospects find the right product and book a demo",
    behavior: { tone: "professional", verbosity: "balanced", format: "bullets", language: "match the user", emoji: false },
    always: ["Ask one clarifying question when the need is unclear"], never: ["Disparage competitors", "Quote prices that are not on our website"],
    guardrails: { escalation: { triggers: ["quote", "contract", "discount"], message: "A sales colleague will contact you with a tailored offer." }, personal_data: "redact" },
    starters: ["Which plan fits a team of 20?", "Can I get a demo?"],
  },
  internal: {
    name: "Internal knowledge", audience: "employees", role: "an internal knowledge assistant",
    goal: "Answer questions about internal policies using the documented sources",
    behavior: { tone: "neutral", verbosity: "balanced", format: "auto", language: "match the user", emoji: false },
    always: ["Cite the source page"], never: ["Share information about individual employees"],
    guardrails: { personal_data: "refuse", disclaimer: "Check the linked policy for the authoritative wording." },
    starters: ["How many vacation days do I have?", "What is the travel policy?"],
  },
};

function normalizeContext(c) {
  c = structuredClone(c || {});
  c.behavior = Object.assign({ tone: "professional", verbosity: "balanced", language: "match the user", emoji: false, format: "auto" }, c.behavior || {});
  for (const k of ["always", "never", "notes", "web_sources", "examples", "starters"]) c[k] = c[k] || [];
  c.guardrails = Object.assign({ allowed_topics: [], blocked_topics: [], refusal_message: "Sorry, I can't help with that topic. Is there something else I can do for you?",
    personal_data: "redact", disclaimer: "" }, c.guardrails || {});
  c.guardrails.escalation = Object.assign({ triggers: [], message: "I'll connect you with a colleague who can help with this." }, c.guardrails.escalation || {});
  for (const k of ["name", "description", "audience", "role", "goal", "owner"]) c[k] = c[k] || "";
  return c;
}

const slug = (s) => (s || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").replace(/^([^a-z])/, "c-$1").slice(0, 40) || "context";

// ------------------------------------------------------------------ functional studio

async function tabStudio(agent) {
  const def = agent.draft.definition;
  const ctxs = def.contexts || [];
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  let selectedId = params.get("ctx") || def.default_context || (ctxs[0] && ctxs[0].id);
  const root = h("div", { class: "studio" });

  const sidebar = () => h("aside", { class: "studio-side" },
    h("div", { class: "side-title" }, "Contexts ", h("span", { class: "muted" }, `(${ctxs.length})`)),
    h("p", { class: "muted small" }, "One agent, several audiences or use cases. Callers pick a context; the default is used otherwise."),
    ...ctxs.map((c) => h("a", { class: "ctx-item" + (c.id === selectedId ? " on" : ""), href: `#/agents/${agent.name}/studio?ctx=${c.id}` },
      h("div", { class: "ctx-name" }, c.name || c.id, c.id === def.default_context ? h("span", { class: "badge ok", style: "margin-left:6px" }, "default") : ""),
      h("div", { class: "muted small" }, c.role || c.audience || c.id))),
    h("button", { class: "primary side-add", onclick: () => newContextDialog() }, "+ New context"));

  const newContextDialog = () => {
    let chosen = "support";
    const nameIn = h("input", { placeholder: "e.g. Retail customers, Branch staff, Dutch-speaking visitors" });
    const tplBox = h("div", { class: "tpl-grid" });
    const renderTpl = () => tplBox.replaceChildren(...Object.entries(TEMPLATES).map(([k, t]) => h("button", {
      class: "tpl-card" + (k === chosen ? " on" : ""), onclick: () => { chosen = k; if (!nameIn.value || Object.values(TEMPLATES).some((x) => x.name === nameIn.value)) nameIn.value = k === "blank" ? "" : t.name; renderTpl(); } },
      h("b", {}, k === "blank" ? "Start blank" : t.name),
      h("div", { class: "muted small" }, k === "blank" ? "Empty context; fill in everything yourself." : `${t.role || ""}${t.starters ? " · e.g. “" + t.starters[0] + "”" : ""}`))));
    renderTpl();
    nameIn.value = TEMPLATES[chosen].name;
    const close = modal("New functional context",
      h("p", { class: "muted" }, "A context is one audience or use case for this agent, with its own role, behaviour, knowledge and boundaries."),
      field("Name", nameIn), h("label", {}, "Start from"), tplBox,
      h("div", { class: "row-btns", style: "justify-content:flex-end; margin-top:12px" },
        h("button", { onclick: () => close() }, "Cancel"),
        h("button", { class: "primary", onclick: () => act(async () => {
          const name = nameIn.value.trim(); if (!name) { nameIn.focus(); return; }
          let id = slug(name); while (ctxs.some((c) => c.id === id)) id += "-2";
          await api("PUT", `/agents/${agent.name}/draft/contexts/${id}`, { context: Object.assign(structuredClone(TEMPLATES[chosen]), { name }), base: "" });
          close(); toast(`Context “${name}” created`);
          location.hash = `#/agents/${agent.name}/studio?ctx=${id}`;
        }) }, "Create context")));
  };

  if (!selectedId) {
    root.append(sidebar(), h("div", { class: "studio-main" }, h("div", { class: "panel" }, h("h2", {}, "No functional context yet"),
      h("p", {}, "Create one from a template on the left. A context describes who the agent is for one audience: its role, behaviour, knowledge and boundaries."))));
    return root;
  }
  const original = ctxs.find((c) => c.id === selectedId);
  if (!original) { location.hash = `#/agents/${agent.name}/studio`; return root; }
  const ctx = normalizeContext(original);
  let base = agent.draft.sections.contexts[selectedId] || "";
  let dirty = false;
  let autosaveTimer = null;
  let saving = false;
  state.dirty = () => dirty;
  const saveState = h("span", { class: "muted" }, "All changes saved");
  const changed = () => {
    dirty = true; saveState.textContent = "Unsaved changes…"; saveState.className = "warn-text"; schedulePreview();
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => { if (dirty && !saving) save(true).catch(() => {}); }, 2500);
  };

  // ---- cards
  const who = card("👤", "Who is this agent?", "Describe it the way you'd brief a new colleague.",
    h("div", { class: "grid2" },
      field("Context name", textInput(ctx, "name", "Retail customers", changed)),
      field("Audience — who will talk to it?", textInput(ctx, "audience", "existing customers on our website", changed))),
    field("Role — “You are …”", textInput(ctx, "role", "a friendly mortgage assistant of Example Bank", changed)),
    field("Goal", textInput(ctx, "goal", "Explain our mortgage products clearly and help people take the next step", changed, true)),
    field("Short description (shown in the chat app)", textInput(ctx, "description", "For customers with questions about mortgages", changed)));

  const b = ctx.behavior;
  const behave = card("🎭", "How should it behave?", "Pick the style; the agent adapts its wording.",
    field("Tone", segmented([["friendly", "Friendly"], ["professional", "Professional"], ["formal", "Formal"], ["empathetic", "Empathetic"], ["playful", "Playful"], ["neutral", "Neutral"]], b.tone, (v) => { b.tone = v; changed(); })),
    field("Answer length", segmented([["brief", "Brief", "1–3 sentences"], ["balanced", "Balanced"], ["detailed", "Detailed"]], b.verbosity, (v) => { b.verbosity = v; changed(); })),
    field("Format", segmented([["auto", "Auto"], ["plain", "Prose"], ["bullets", "Bullets"], ["steps", "Numbered steps"]], b.format, (v) => { b.format = v; changed(); })),
    h("div", { class: "grid2" },
      field("Language", (() => { const i = textInput(b, "language", "match the user", changed); i.setAttribute("list", "langs"); return i; })()),
      field("Emoji", toggle(b.emoji, "Allow occasional emoji", (v) => { b.emoji = v; changed(); }))),
    h("datalist", { id: "langs" }, ["match the user", "English", "Dutch", "German", "French", "Spanish", "Hindi"].map((l) => h("option", { value: l }))));

  const rules = card("✅", "Always do / Never do", "Short, concrete rules work best. Press Enter after each.",
    h("div", { class: "grid2" },
      field("Always", chips(ctx.always, "e.g. Offer a next step", changed)),
      field("Never", chips(ctx.never, "e.g. Give personal financial advice", changed))));

  // knowledge: web sources + notes
  const webBox = h("div", {});
  const scanBox = h("div", { class: "scan-status" });
  const renderWeb = () => webBox.replaceChildren(...ctx.web_sources.map((w, i) => h("div", { class: "web-row" },
    (() => { const u = h("input", { value: w.url, placeholder: "https://www.example.com/products" }); u.addEventListener("input", () => { w.url = u.value.trim(); changed(); }); return u; })(),
    (() => { const n = h("input", { type: "number", min: 1, max: 25, value: w.max_pages || 5, title: "max pages", class: "num" }); n.addEventListener("input", () => { w.max_pages = Number(n.value || 5); changed(); }); return n; })(),
    toggle(w.same_domain_only !== false, "same site only", (v) => { w.same_domain_only = v; changed(); }),
    h("button", { class: "danger", onclick: () => { ctx.web_sources.splice(i, 1); changed(); renderWeb(); } }, "Remove"))));
  renderWeb();
  const loadScans = async () => {
    try {
      const { scans } = await api("GET", `/agents/${agent.name}/contexts/${selectedId}/scans`);
      const s = scans[0];
      if (s && s.status === "running") {
        scanBox.replaceChildren(h("span", { class: "warn-text" }, "⏳ Scanning web pages… you can keep working; this updates by itself."));
        setTimeout(() => { if (document.body.contains(scanBox)) loadScans(); }, 2000);
        return;
      }
      scanBox.replaceChildren(s ? h("span", {}, statusBadge(s.status), ` last scan ${s.started_at.slice(0, 16).replace("T", " ")} · ${s.pages} page(s), ${s.chunks} chunk(s)`,
        s.details && s.details.warnings && s.details.warnings.length ? h("div", { class: "warn-text small" }, s.details.warnings.join("; ")) : "",
        s.details && s.details.report ? h("details", {}, h("summary", {}, "Pages"), h("ul", {}, s.details.report.map((p) => h("li", { class: p.ok ? "" : "fail" }, p.ok ? `${p.title || p.url} — ${p.chars} chars` : `${p.url}: ${p.error}`)))) : "")
        : h("span", { class: "muted" }, "Not scanned yet."));
    } catch (e) { scanBox.replaceChildren(); }
  };
  loadScans();
  const notesBox = h("div", {});
  const renderNotes = () => notesBox.replaceChildren(...ctx.notes.map((n, i) => h("div", { class: "note" },
    textInput(n, "title", "Title, e.g. Opening hours", changed), textInput(n, "text", "What the agent should know", changed, true),
    h("button", { class: "danger small-btn", onclick: () => { ctx.notes.splice(i, 1); changed(); renderNotes(); } }, "Remove"))));
  renderNotes();
  const know = card("📚", "What it knows", "Web pages are fetched and indexed so answers can cite them. Notes are facts the agent always knows.",
    h("h4", {}, "Web pages to learn from"), webBox,
    h("div", { class: "row-btns" },
      h("button", { onclick: () => { ctx.web_sources.push({ url: "", max_pages: 5, same_domain_only: true }); changed(); renderWeb(); } }, "+ Web page"),
      h("button", { class: "primary", onclick: () => act(async () => {
        if (dirty) { await save(false); }
        await api("POST", `/agents/${agent.name}/contexts/${selectedId}/scan`);
        loadScans();
      }) }, "Scan now")),
    scanBox,
    h("h4", {}, "Facts & FAQ"), notesBox,
    h("button", { onclick: () => { ctx.notes.push({ title: "", text: "" }); changed(); renderNotes(); } }, "+ Note"));

  const g = ctx.guardrails;
  const bounds = card("🛡️", "Boundaries", "These are enforced in code before the model is called, and answered politely.",
    field("Stick to these topics (the agent politely declines others)", chips(g.allowed_topics, "e.g. mortgages", changed)),
    h("div", { class: "grid2" },
      field("Refuse these topics", chips(g.blocked_topics, "e.g. crypto", changed)),
      field("Refusal reply", textInput(g, "refusal_message", "", changed, true))),
    h("div", { class: "grid2" },
      field("Hand over to a human when the user mentions", chips(g.escalation.triggers, "e.g. complaint", changed)),
      field("Hand-over reply", textInput(g.escalation, "message", "", changed, true))),
    field("Personal data shared by users", segmented([["redact", "Redact it", "Mask emails, phone numbers, IBANs, cards"], ["refuse", "Ask not to share", "Reply with a privacy note"], ["allow", "Allow"]], g.personal_data, (v) => { g.personal_data = v; changed(); })),
    field("Disclaimer added to every answer (optional)", textInput(g, "disclaimer", "This is general information, not advice.", changed)));

  const exBox = h("div", {});
  const renderEx = () => exBox.replaceChildren(...ctx.examples.map((e, i) => h("div", { class: "example" },
    field("User says", textInput(e, "user", "", changed)), field("Ideal answer", textInput(e, "assistant", "", changed, true)),
    h("button", { class: "danger small-btn", onclick: () => { ctx.examples.splice(i, 1); changed(); renderEx(); } }, "Remove"))));
  renderEx();
  const examples = card("💬", "Examples & conversation starters", "Examples teach style. Starters appear as buttons in the chat app and seed the smoke test.",
    exBox, h("button", { onclick: () => { ctx.examples.push({ user: "", assistant: "" }); changed(); renderEx(); } }, "+ Example"),
    field("Conversation starters", chips(ctx.starters, "e.g. How much can I borrow?", changed)));

  const anchors = { role: who, goal: who, audience: who, "always / never": rules, "knowledge (web or notes)": know,
    boundaries: bounds, escalation: bounds, examples, starters: examples };

  // ---- save
  const draftWith = () => {
    const d = structuredClone(agent.draft.definition);
    d.contexts = (d.contexts || []).map((c) => (c.id === selectedId ? Object.assign({}, ctx, { id: selectedId }) : c));
    return d;
  };
  const save = async (auto) => {
    if (!ctx.name.trim()) { saveState.textContent = "Name required to save"; return; }
    saving = true;
    try {
      const d = await api("PUT", `/agents/${agent.name}/draft/contexts/${selectedId}`, { context: ctx, base });
      base = d.sections.contexts[selectedId]; agent.draft = d; dirty = false;
      saveState.textContent = auto ? `Autosaved ${new Date().toLocaleTimeString()}` : "All changes saved"; saveState.className = "muted";
      if (!auto) toast("Context saved");
    } catch (e) {
      saveState.textContent = "Not saved"; saveState.className = "fail";
      if (e.status === 409) toast("Someone else changed this context since you opened it. Copy your edits, then reload.", true);
      else if (auto) toast("Autosave failed: " + e.message, true);
      else throw e;
    } finally { saving = false; }
  };
  const header = h("div", { class: "studio-head panel" },
    h("div", {}, h("h2", {}, ctx.name || selectedId), h("div", { class: "muted small" }, `context id: ${selectedId}`)),
    h("div", { class: "row-btns" }, saveState,
      def.default_context !== selectedId ? h("button", { onclick: () => act(async () => { await api("PUT", `/agents/${agent.name}/draft/default-context`, { context: selectedId }); toast("Default context set"); route(); }) }, "Make default") : "",
      h("button", { class: "danger", onclick: () => act(async () => { if (!confirm(`Delete context “${ctx.name}”?`)) return; await api("DELETE", `/agents/${agent.name}/draft/contexts/${selectedId}`); location.hash = `#/agents/${agent.name}/studio`; }) }, "Delete"),
      h("button", { class: "primary", onclick: () => act(() => save(false)) }, "Save now")));

  // ---- right panel: completeness, live preview, what the model sees
  const meter = h("div", { class: "meter" });
  const promptBox = h("pre", { class: "prompt" });
  const chatLog = h("div", { class: "chat-log" });
  const chatInput = h("input", { placeholder: "Ask something as a user would…" });
  const storeKey = `eap.preview.${agent.name}.${selectedId}`;
  let history = [];
  try { history = JSON.parse(sessionStorage.getItem(storeKey) || "[]"); } catch { history = []; }
  const persist = () => { try { sessionStorage.setItem(storeKey, JSON.stringify(history.slice(-40))); } catch {} };
  let compareId = "";
  const compareSel = select([["", "— none —"], ...ctxs.filter((c) => c.id !== selectedId).map((c) => [c.id, c.name || c.id])], "", (v) => { compareId = v; });
  let previewTimer = null;
  function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(refreshCompose, 600); }
  async function refreshCompose() {
    try {
      const r = await api("POST", `/agents/${agent.name}/compose?context=${selectedId}`, { definition: draftWith() });
      promptBox.textContent = r.system_prompt;
      const c = r.completeness;
      meter.replaceChildren(h("div", { class: "meter-bar" }, h("div", { class: "meter-fill", style: `width:${c.score}%` })),
        h("div", { class: "small" }, h("b", {}, `${c.score}% complete`), " · ",
          Object.entries(c.checks).map(([k, ok]) => ok ? h("span", { class: "pass" }, "✓ " + k + "  ")
            : h("button", { class: "linklike", title: "Go to this section", onclick: () => anchors[k] && anchors[k].scrollIntoView({ behavior: "smooth", block: "start" }) }, "○ " + k))),
        (c.hints || []).length ? h("ul", { class: "hints" }, c.hints.map((x) => h("li", {}, "💡 ", x))) : "");
    } catch (e) { /* invalid in-progress edits: keep last good preview */ }
  }
  const bubble = (role, text, extra) => h("div", { class: "bubble " + role },
    role === "assistant" ? mdLite(text) : h("div", { class: "bubble-text" }, text), extra || "");
  // "Save as test": turn an answer you like (or dislike) into a regression test, in plain words.
  const saveAsTest = (msg, text, flags, anchor) => {
    const judge = h("input", { type: "checkbox", checked: !flags.length });
    const flagBox = h("input", { type: "checkbox", checked: flags.length > 0 });
    const must = h("input", { placeholder: "comma separated, e.g. 1.75%, savings" });
    const mustNot = h("input", { placeholder: "comma separated, e.g. guarantee, promise" });
    const form = h("div", { class: "save-test" },
      h("b", {}, "Save as test"), h("div", { class: "muted small" }, `When a user says “${msg.slice(0, 80)}”, the agent must…`),
      h("label", { class: "toggle" }, judge, h("span", {}, "give an answer like this one (AI judge compares)")),
      flags.length ? h("label", { class: "toggle" }, flagBox, h("span", {}, "be handled the same way: " + flags.map(humanFlag).join(", "))) : "",
      field("mention", must), field("not mention", mustNot),
      h("div", { class: "row-btns" }, h("button", { class: "small-btn", onclick: () => form.remove() }, "Cancel"),
        h("button", { class: "small-btn primary", onclick: () => act(async () => {
          const assertions = [];
          if (judge.checked) assertions.push({ type: "llm_judge", threshold: 0.7, model: def.model.catalog_id,
            rubric: `The answer gives the same key information and follows the same approach as this approved answer: "${text.slice(0, 700)}"` });
          if (flags.length && flagBox.checked) flags.forEach((f) => assertions.push({ type: "contains", path: "flags", value: f }));
          must.value.split(",").map((x) => x.trim()).filter(Boolean).forEach((v) => assertions.push({ type: "contains", path: "output", value: v }));
          mustNot.value.split(",").map((x) => x.trim()).filter(Boolean).forEach((v) => assertions.push({ type: "not_contains", path: "output", value: v }));
          const r = await api("POST", `/agents/${agent.name}/evals/cases`, { case: { context: selectedId, input: { message: msg }, assertions } });
          form.replaceWith(h("div", { class: "pass small" }, `✓ Saved as test ${r.case_id} in “${r.suite}” (${r.cases} test(s)) — runs in Release → ① Test.`));
        }) }, "Save test")));
    anchor.after(form);
  };

  const answerMeta = (r, msg, text, label) => {
    const meta = h("div", { class: "bubble-meta" },
      label ? badge(label) : "",
      (r.flags || []).map((f) => badge(humanFlag(f), "warn")), " ",
      (r.sources || []).map((s, i) => h("a", { href: s.uri, target: "_blank", rel: "noopener", class: "src" }, `[${i + 1}] ${s.title || s.uri}`)),
      (r.warnings || []).length ? h("div", { class: "muted small" }, r.warnings.join("; ")) : "",
      timelineView(r));
    if (!label && r.status !== "failed") meta.append(h("button", { class: "linklike small", onclick: (e) => saveAsTest(msg, text, r.flags || [], e.target.closest(".bubble")) }, "＋ Save as test"));
    return meta;
  };
  const ask = async (contextId, msg, hist) => {
    const r = await api("POST", `/agents/${agent.name}/preview`, { context: contextId, message: msg, history: hist, definition: draftWith() });
    const text = r.status === "failed" ? `⚠ ${r.error ? explainError(r.error).title + (explainError(r.error).fix ? " " + explainError(r.error).fix : "") : "failed"}` : (typeof r.output === "string" ? r.output : JSON.stringify(r.output, null, 2));
    return [r, text];
  };
  const send = () => act(async () => {
    const msg = chatInput.value.trim(); if (!msg) return;
    chatInput.value = "";
    chatLog.append(bubble("user", msg));
    const thinking = bubble("assistant", "…"); chatLog.append(thinking); chatLog.scrollTop = chatLog.scrollHeight;
    try {
      const hist = history.slice(-20).map(({ role, content }) => ({ role, content }));
      const cmpId = compareId;
      const [main, other] = await Promise.all([ask(selectedId, msg, hist), cmpId ? ask(cmpId, msg, hist) : Promise.resolve(null)]);
      const [r, text] = main;
      thinking.replaceWith(bubble("assistant", text, answerMeta(r, msg, text, cmpId ? (ctx.name || selectedId) : "")));
      if (other) {
        const c2 = ctxs.find((c) => c.id === cmpId);
        chatLog.append(h("div", { class: "bubble assistant compare" }, mdLite(other[1]), answerMeta(other[0], msg, other[1], "compare: " + (c2 ? c2.name : cmpId))));
      }
      history.push({ role: "user", content: msg }, { role: "assistant", content: text, flags: r.flags || [] }); persist();
    } catch (e) { thinking.replaceWith(bubble("assistant", "⚠ " + e.message)); }
    chatLog.scrollTop = chatLog.scrollHeight;
  });
  // restore the previous preview conversation for this context
  for (const t of history) chatLog.append(t.role === "user" ? bubble("user", t.content) : bubble("assistant", t.content, h("div", { class: "bubble-meta muted small" }, "(earlier in this session)")));
  chatInput.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  const starterBtns = h("div", { class: "starters" });
  const renderStarters = () => starterBtns.replaceChildren(...ctx.starters.slice(0, 4).map((s) => h("button", { class: "starter", onclick: () => { chatInput.value = s; send(); } }, s)));
  renderStarters();
  const right = h("aside", { class: "studio-preview" },
    h("div", { class: "panel" }, h("h3", {}, "Completeness"), meter),
    h("div", { class: "panel" }, h("div", { class: "row-btns", style: "justify-content:space-between" }, h("h3", { style: "margin:0" }, "Live preview"),
        h("button", { class: "small-btn", onclick: () => { history.length = 0; persist(); chatLog.replaceChildren(); } }, "Reset")),
      h("p", { class: "muted small" }, "Talks to your draft — including unsaved edits — through the runtime. Nothing is published."),
      ctxs.length > 1 ? h("div", { class: "row compare-row" }, field("Compare with context", compareSel)) : "",
      chatLog, starterBtns, h("div", { class: "chat-input" }, chatInput, h("button", { class: "primary", onclick: send }, "Send"))),
    h("div", { class: "panel" }, h("details", {}, h("summary", {}, h("b", {}, "What the model sees")),
      h("p", { class: "muted small" }, "System-level context (technical) + this functional context + platform rules."), promptBox)));
  refreshCompose();

  window.onbeforeunload = () => (dirty ? "You have unsaved changes" : undefined);
  root.append(sidebar(), h("div", { class: "studio-main" }, header, who, behave, rules, know, bounds, examples), right);
  return root;
}

// ------------------------------------------------------------------ overview

// Why is (or isn't) this agent in the chat app? Checks the PUBLISHED prod version, not the draft.
function chatVisibility(agent) {
  const box = h("div", { class: "chat-vis" }, h("span", { class: "muted small" }, "Checking chat app visibility…"));
  const prod = agent.aliases.find((a) => a.alias === "prod");
  const draftChat = !!(agent.draft.definition.exposure || {}).chat;
  const show = (ok, lines) => box.replaceChildren(
    h("div", { class: ok ? "pass" : "warn-text" }, ok ? "✓ Visible in the chat app" : "✗ Not visible in the chat app"),
    lines.length ? h("ul", { class: "small" }, lines.map((l) => h("li", {}, l))) : "");
  if (!prod) {
    show(false, ["No version is live: nothing is pointed at the “prod” alias yet. Use ⚡ Quick publish, or Publish with alias “prod”."]);
    return box;
  }
  api("GET", `/agents/${agent.name}/versions/${prod.version}`).then((v) => {
    const live = !!((v.definition.exposure || {}).chat);
    const lines = [];
    if (!live) lines.push(`The live version ${prod.version} has “Offer in chat app” switched off.` +
      (draftChat ? " The draft has it on — publish again to make it live." : " Turn it on in Technical setup → Basics, save, then publish."));
    else lines.push(`prod → ${prod.version}. Reload the chat app (it refreshes its agent list on load).`);
    show(live, lines);
  }).catch((e) => show(false, [e.message]));
  return box;
}

async function tabOverview(agent) {
  const d = agent.draft.definition;
  const model = state.catalog.models.find((m) => m.id === d.model.catalog_id);
  const latest = agent.versions[0];
  const quick = h("div", {});
  const canQuick = state.settings && state.settings.relaxed_release;
  return h("div", { class: "overview" },
    h("div", { class: "ov-grid" },
      h("div", { class: "panel" }, h("h3", {}, "🧑‍💼 Functional (business)"),
        h("p", { class: "muted small" }, "Role, behaviour, knowledge and boundaries per audience."),
        (d.contexts || []).length ? h("ul", { class: "plain" }, d.contexts.map((c) => h("li", {}, h("a", { href: `#/agents/${agent.name}/studio?ctx=${c.id}` }, c.name || c.id),
          c.id === d.default_context ? badge("default", "ok") : "", h("div", { class: "muted small" }, c.role || "no role yet")))) : h("p", {}, "No contexts yet."),
        h("a", { class: "btn-link", href: `#/agents/${agent.name}/studio` }, "Open functional studio →")),
      h("div", { class: "panel" }, h("h3", {}, "🛠️ Technical"),
        h("p", { class: "muted small" }, "System-level context, model, orchestration, tools, limits."),
        h("table", {},
          h("tr", {}, h("td", {}, "Model"), h("td", {}, d.model.catalog_id, " ", model ? statusBadge(model.status) : badge("missing", "bad"))),
          h("tr", {}, h("td", {}, "Pattern"), h("td", {}, d.pattern.type)),
          h("tr", {}, h("td", {}, "Tools / agents"), h("td", {}, `${d.bindings.tools.length} / ${d.bindings.agents.length}`)),
          h("tr", {}, h("td", {}, "Channels (draft)"), h("td", {}, [d.exposure.chat ? "chat app" : "", d.exposure.mcp ? "MCP" : "", "API"].filter(Boolean).join(", ")))),
        chatVisibility(agent),
        h("a", { class: "btn-link", href: `#/agents/${agent.name}/technical` }, "Open technical setup →")),
      h("div", { class: "panel" }, h("h3", {}, "🚀 Release"),
        h("table", {},
          h("tr", {}, h("td", {}, "Latest version"), h("td", {}, latest ? `${latest.version} (${latest.published_at.slice(0, 16).replace("T", " ")})` : "—")),
          h("tr", {}, h("td", {}, "Aliases"), h("td", {}, agent.aliases.map((a) => h("div", {}, h("code", {}, a.alias), " → ", a.version)))),
          h("tr", {}, h("td", {}, "Draft edited by"), h("td", {}, agent.draft.editors.join(", ") || "—"))),
        canQuick ? h("div", {},
          h("p", { class: "muted small" }, "Dev mode: run evals, approve, publish and point ", h("code", {}, "prod"), " in one step. Every step is audited."),
          h("button", { class: "primary", onclick: () => act(async () => {
            quick.replaceChildren(h("p", { class: "muted" }, "Running evals and publishing…"));
            try {
              const r = await api("POST", `/agents/${agent.name}/quick-publish`, {});
              quick.replaceChildren(h("p", { class: "pass" }, `✓ ${r.published ? "Published" : "Re-pointed"} ${r.version} → prod`, r.smoke_suite_created ? " (smoke suite created from your starters)" : ""),
                state.settings.chat_url ? h("a", { href: state.settings.chat_url, target: "_blank", rel: "noopener" }, "Open the chat app →") : "");
            } catch (e) {
              quick.replaceChildren(h("p", { class: "fail" }, e.message), e.details ? pre(e.details) : "");
            }
          }) }, "⚡ Quick publish to prod"), quick)
          : h("a", { class: "btn-link", href: `#/agents/${agent.name}/release` }, "Go to release →"))));
}

window.tabStudio = tabStudio;
window.tabOverview = tabOverview;
