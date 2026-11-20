"use strict";
const $ = (id) => document.getElementById(id);
const state = { me: null, bu: null, agents: [], agent: null, context: null, history: [], session: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) };
const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (k === "class") e.className = v; else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}

// Minimal, safe markdown: escape first, then **bold**, _italic_, `code`, [n] citations, links, lists, paragraphs.
function md(text) {
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_(.+?)_(?=[\s).,!?]|$)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = []; let list = null;
  for (const raw of String(text).split("\n")) {
    const line = raw.trimEnd();
    const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (ul || ol) {
      const type = ul ? "ul" : "ol";
      if (!list || list.type !== type) { if (list) out.push(`</${list.type}>`); out.push(`<${type}>`); list = { type }; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`); continue;
    }
    if (list) { out.push(`</${list.type}>`); list = null; }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list.type}>`);
  const div = document.createElement("div"); div.innerHTML = out.join(""); return div;
}

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error((data.error && data.error.message) || r.statusText); e.status = r.status; e.login = data.login; throw e; }
  return data;
}

const ICON = { guardrail: "🛡️", retrieval: "📚", model: "🧠", tool: "🔧", agent: "🤖" };
function stepText(st) {
  const d = st.detail || {};
  if (st.kind === "model") return `${st.name}: ${d.input_tokens || 0}→${d.output_tokens || 0} tokens` +
    (d.tool_calls && d.tool_calls.length ? ` · decided to call ${d.tool_calls.join(", ")}` : " · answered") + (d.error ? ` · ${d.error}` : "");
  if (st.kind === "tool") return `${st.name}(${JSON.stringify(d.arguments || {}).slice(0, 100)}) → ${String(d.result_preview || "").slice(0, 120)}`;
  if (st.kind === "retrieval") return `searched ${st.name}: ${d.hits ?? 0} passage(s)`;
  if (st.kind === "agent") return `called agent ${st.name}`;
  if (st.kind === "guardrail") return `${st.name} ${st.status}${d.stage ? " at " + d.stage : ""}`;
  return st.name;
}
function timeline(r) {
  if (!r.steps || !r.steps.length) return null;
  return el("details", { class: "how" }, el("summary", {}, `How this answer was made · ${r.steps.length} step(s)`),
    el("ol", {}, ...r.steps.map((st) => el("li", {}, `${ICON[st.kind] || "•"} ${stepText(st)}`, st.latency_ms ? el("span", { class: "muted" }, ` · ${st.latency_ms} ms`) : null))));
}

function renderAgents() {
  const box = $("agents");
  if (!state.agents.length) { box.replaceChildren(el("p", { class: "muted small" }, "No chat-enabled agents are published to prod yet.")); return; }
  box.replaceChildren(...state.agents.map((a) => el("div", { class: "agent" }, el("h3", {}, a.display_name || a.name,
      a.edition ? el("span", { class: "edition", title: `Tailored for ${state.bu.name}` }, state.bu.name) : null), el("p", {}, a.description || ""),
    ...(a.contexts.length ? a.contexts : [{ id: null, name: "Default" }]).map((c) => el("button", {
      class: "ctx" + (state.agent === a && state.context === c.id ? " on" : ""),
      onclick: () => select(a, c.id) }, c.name, c.description ? el("small", {}, c.description) : null)))));
}

function select(agent, contextId) {
  state.agent = agent; state.context = contextId || agent.default_context; state.history = [];
  state.session = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  const ctx = agent.contexts.find((c) => c.id === state.context);
  $("agentTitle").textContent = agent.display_name || agent.name;
  $("agentSub").textContent = ctx ? `Context: ${ctx.name}` : "";
  $("input").disabled = $("send").disabled = false;
  $("log").replaceChildren(el("div", { class: "empty" }, state.bu.welcome ? el("p", { class: "welcome" }, state.bu.welcome) : null,
    el("p", {}, `You're chatting with ${agent.display_name || agent.name}${ctx ? " · " + ctx.name : ""} for ${state.bu.name}.`),
    el("p", { class: "small" }, "Answers come from the published version of the agent. Your messages are processed on your behalf.")));
  $("starters").replaceChildren(...((ctx && ctx.starters) || []).slice(0, 4).map((s) => el("button", { onclick: () => send(s) }, s)));
  renderAgents(); $("side").classList.remove("open"); $("input").focus();
}

function rate(runId) {
  const box = el("div", { class: "rate" });
  const done = (txt) => box.replaceChildren(el("span", { class: "small muted" }, txt));
  const post = async (rating, comment) => {
    try { await api("POST", "/api/feedback", { run_id: runId, rating, comment }); done(rating === "up" ? "Thanks for the feedback 👍" : "Thanks — a colleague will review this answer."); }
    catch (e) { done("⚠ " + e.message); }
  };
  const down = () => {
    const input = el("input", { placeholder: "What was wrong? (optional)", maxlength: "500" });
    box.replaceChildren(input, el("button", { class: "ghost", onclick: () => post("down", input.value) }, "Send"),
      el("button", { class: "ghost", onclick: () => post("down", "") }, "Skip"));
    input.focus();
  };
  box.append(el("button", { class: "thumb", title: "Helpful", onclick: () => post("up", "") }, "👍"),
    el("button", { class: "thumb", title: "Not helpful", onclick: down }, "👎"));
  return box;
}

async function send(text) {
  const msg = (text || $("input").value).trim();
  if (!msg || !state.agent) return;
  $("input").value = ""; $("starters").replaceChildren();
  const log = $("log");
  if (log.querySelector(".empty")) log.replaceChildren();
  log.append(el("div", { class: "msg user" }, msg));
  const typing = el("div", { class: "msg bot" }, el("span", { class: "typing" }, el("span"), el("span"), el("span")));
  log.append(typing); log.scrollTop = log.scrollHeight;
  $("send").disabled = true;
  try {
    const r = await api("POST", "/api/chat", { bu: state.bu.id, agent: state.agent.name, context: state.context, message: msg,
      history: state.history.slice(-20), session_id: state.session });
    const text = r.status === "failed" ? `Sorry, something went wrong: ${r.error || "unknown error"}` :
      (typeof r.reply === "string" ? r.reply : JSON.stringify(r.reply, null, 2));
    const bubble = el("div", { class: "msg bot" }, md(text),
      el("div", { class: "meta" },
        ...r.sources.map((s, i) => el("a", { href: s.uri, target: "_blank", rel: "noopener" }, `[${i + 1}] ${s.title || s.uri}`)),
        el("span", {}, `v${r.version} · ${r.latency_ms} ms`)), timeline(r), r.run_id ? rate(r.run_id) : "");
    typing.replaceWith(bubble);
    if (r.flags.includes("escalated")) log.append(el("div", { class: "msg notice" }, "🙋 This conversation was flagged for a human colleague."));
    state.history.push({ role: "user", content: msg }, { role: "assistant", content: text });
  } catch (e) {
    typing.replaceWith(el("div", { class: "msg notice" }, `⚠ ${e.message}`));
  } finally {
    $("send").disabled = false; log.scrollTop = log.scrollHeight; $("input").focus();
  }
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("input").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px"; });
$("newChat").addEventListener("click", () => state.agent && select(state.agent, state.context));
$("menu").addEventListener("click", () => $("side").classList.toggle("open"));

// ------------------------------------------------------------------ sign-in and business unit

function gate(...content) {
  $("app").hidden = true; $("gate").hidden = false;
  $("gate").replaceChildren(el("div", { class: "gate-card" }, el("div", { class: "brand" }, "💬 ", (state.me && state.me.title) || "Agent Chat"), ...content));
}

function loginScreen(kind, message) {
  if (kind !== "dev") {
    gate(el("p", {}, "Please sign in with your company account."), el("p", { class: "small muted" }, message || ""),
      el("button", { class: "send", onclick: () => location.reload() }, "Sign in"));
    return;
  }
  const email = el("input", { type: "email", placeholder: "name@company.com", autocomplete: "username" });
  const msg = el("p", { class: "small muted" }, "Local development sign-in. In production, people sign in with their Google account.");
  const go = async () => {
    try { await api("POST", "/api/login", { email: email.value }); boot(); }
    catch (e) { msg.textContent = "⚠ " + e.message; }
  };
  email.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  gate(el("h2", {}, "Sign in"), email, el("button", { class: "send", onclick: go }, "Continue"), msg);
  email.focus();
}

async function signOut() {
  try { const r = await api("POST", "/api/logout"); store.set("eap.chat.bu", ""); location.href = r.redirect || "/"; } catch { location.href = "/"; }
}

function buPicker() {
  const me = state.me;
  gate(el("h2", {}, `Hello ${me.display_name || me.user.split("@")[0]}`),
    el("p", {}, "Which business unit are you working for today?"),
    el("div", { class: "bu-list" }, ...me.bus.map((b) => el("button", { class: "bu-choice" + (b.id === me.default_bu ? " default" : ""),
      onclick: () => chooseBu(b.id) }, el("b", {}, b.name), b.id === me.default_bu ? el("small", {}, "your default") : null))),
    el("p", { class: "small muted" }, "Agents adapt to your business unit: its own edition, rules and knowledge."),
    me.sign_out ? el("button", { class: "linkish", onclick: signOut }, "Sign out") : null);
}

async function chooseBu(id) {
  state.bu = state.me.bus.find((b) => b.id === id);
  store.set("eap.chat.bu", id);
  $("gate").hidden = true; $("app").hidden = false;
  $("buRow").replaceChildren(el("span", { class: "bu-chip", title: "Business unit you are working for" }, "🏢 ", state.bu.name),
    state.me.bus.length > 1 ? el("button", { class: "linkish", onclick: buPicker }, "switch") : null,
    state.me.sign_out ? el("button", { class: "linkish", onclick: signOut }, "sign out") : null);
  state.agent = null; $("agentTitle").textContent = "Choose an agent"; $("agentSub").textContent = "";
  $("log").replaceChildren(el("div", { class: "empty" }, state.bu.welcome ? el("p", { class: "welcome" }, state.bu.welcome) : null,
    el("p", {}, "Pick an agent on the left to start.")));
  $("input").disabled = $("send").disabled = true; $("starters").replaceChildren();
  $("agents").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    state.agents = (await api("GET", `/api/agents?bu=${encodeURIComponent(id)}&refresh=1`)).agents;
    renderAgents();
    if (state.agents.length === 1) select(state.agents[0], state.agents[0].default_context);
  } catch (e) { $("agents").replaceChildren(el("p", { class: "muted" }, "⚠ " + e.message)); }
}

async function boot() {
  try {
    state.me = await api("GET", "/api/me");
  } catch (e) {
    if (e.status === 401) { loginScreen(e.login, e.message); return; }
    gate(el("p", {}, "⚠ " + e.message)); return;
  }
  const me = state.me;
  $("who").textContent = (me.display_name ? me.display_name + " · " : "") + me.user;
  $("title").textContent = me.title; document.title = me.title;
  if (!me.bus.length) {
    gate(el("h2", {}, "No business unit yet"), el("p", {}, `You are signed in as ${me.user}, but you are not a member of any business unit.`),
      el("p", { class: "small muted" }, "Ask your BU admin to add you (Governor → People)."),
      me.sign_out ? el("button", { class: "linkish", onclick: signOut }, "Sign out") : null);
    return;
  }
  const remembered = store.get("eap.chat.bu");
  if (remembered && me.bus.some((b) => b.id === remembered)) return chooseBu(remembered);
  if (me.bus.length === 1) return chooseBu(me.bus[0].id);
  buPicker();
}

boot();
