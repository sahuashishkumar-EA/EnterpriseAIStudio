// Monitor: what happens after go-live. Inbox (hand-overs and 👎 answers to review), Runs explorer
// (every live conversation turn with its orchestration timeline) and a Dashboard per agent.
// Also: plain-language explanations for API errors, used by every screen through fail().
"use strict";

// ------------------------------------------------------------------ plain-language errors

const ROLE_LABELS = { viewer: "Viewer", functional_author: "Functional author", author: "Technical author",
  approver: "Approver", publisher: "Publisher", bu_admin: "BU admin", platform_admin: "Platform admin" };

// [code or message pattern, (match) => [what happened, what to do]]
const ERROR_HELP = [
  [/lacks role (\w+) for BU (\S+)/, (m) => [`You need the “${ROLE_LABELS[m[1]] || m[1]}” role for business unit “${m[2]}”.`, "Ask a BU admin to grant it on the Roles screen."]],
  [/platform_admin required/, () => ["Only platform admins can do this.", "Ask a platform admin, or do it from their account."]],
  [/changed by someone else/, () => ["Someone else saved this section after you opened it.", "Copy your edits, reload the page and apply them again."]],
  [/draft changed since/, () => ["The draft changed after it was tested or reviewed.", "Run the tests again on the latest changes (Release → ① Test)."]],
  [/separation of duties/, () => ["You edited this draft, so someone else has to approve it.", "Ask another approver. For prototypes, dev mode (EAP_RELAXED_RELEASE) allows self-approval."]],
  [/evals did not pass/, () => ["The tests did not pass.", "Open Release → ① Test to see which cases failed and why."]],
  [/already exists/, () => ["That name is already taken.", "Choose another name, or open the existing one from the list."]],
  [/no web sources/, () => ["This context has no web pages to scan.", "Add page URLs under “Web pages to learn from”, save, then scan."]],
  [/unknown business unit|unknown BU/i, () => ["That business unit does not exist yet.", "Pick an existing one; only platform admins can create new business units."]],
  [/Publisher model .* not found|model .* (was )?not found|404 .*model/i, () => ["The language model could not be found.", "Check the model ID and region in Catalog → Models and press Test."]],
  [/API key|PERMISSION_DENIED|api_key/i, () => ["The model provider rejected the credentials.", "Check the API key secret (google_ai) or the service account's Vertex AI access."]],
  [/runtime_unavailable|runtime .* failed|Could not reach the agent runtime/, () => ["The agent runtime could not be reached.", "Check that the runtime service is running (locally: scripts/run-local.sh)."]],
  [/governor unreachable/, () => ["The runtime could not reach the governor.", "Check the governor URL and the runtime's service account."]],
  ["unauthenticated", () => ["You are not signed in.", "Reload the page. In local dev, set a dev user in the top bar."]],
  ["budget_exceeded", () => ["The run hit its cost limit.", "Raise “Max cost per run” in Technical → Limits (within policy), or shorten the conversation."]],
  ["depth_exceeded", () => ["Too many agents called each other in a chain.", "Reduce the number of nested sub-agents; the depth limit is set by policy."]],
  ["cycle_detected", () => ["Agents call each other in a loop.", "Remove one of the sub-agent links so the calls form a chain, not a circle."]],
  ["guardrail_blocked", () => ["A guardrail stopped this answer.", "Open the timeline to see which guardrail fired; adjust it or the instructions."]],
  ["model_refused", () => ["The language model refused to answer.", "Rephrase the instructions or check the model's safety settings."]],
  ["model_error", () => ["The language model call failed.", "Try again; if it persists, test the model in Catalog → Models."]],
  ["invalid_output", () => ["The answer did not match the agent's output format.", "Relax the Interface (contract) or describe the format in the instructions."]],
  ["invalid_input", () => ["The question did not match the agent's input format.", "Check the Interface (contract) input fields."]],
  ["invalid_dataset", () => ["The test file could not be read.", "Use a JSON array, JSONL or CSV with an “input” column (see examples/datasets)."]],
  ["not_waiting", () => ["This run is not waiting for an answer anymore.", "Start a new conversation."]],
  ["not_found", () => ["It does not exist (anymore).", "It may have been renamed or deleted. Go back and reload."]],
  ["forbidden", () => ["You are not allowed to do this.", "Ask a BU admin for the right role on the Roles screen."]],
  [/(\d+) test\(s\) would fail before the agent runs[^.]*\. (Each test's input must look like [^|]*?\.) ([\s\S]*)/, (m) => [
    `${m[1]} test(s) can't run as written: the input doesn't match what this agent takes.`,
    `${m[2]}\nFix these and save again:\n• ${m[3].split(" | ").join("\n• ")}`]],
  ["invalid", () => ["Some values are not valid.", "See the details below and correct the highlighted fields."]],
];

function explainError(e) {
  const code = e.code || "", msg = String(e.message || "");
  for (const [k, fn] of ERROR_HELP) {
    const m = typeof k === "string" ? (code === k ? [k] : null) : (msg.match(k) || code.match(k));
    if (m) { const [title, fix] = fn(m); return { title, fix, raw: msg }; }
  }
  return { title: msg || "Something went wrong.", fix: "", raw: "" };
}

function errorView(e) {
  const x = explainError(e);
  let raw = x.raw && x.raw !== x.title ? x.raw : "";
  if (e.details) raw += (raw ? "\n" : "") + (typeof e.details === "string" ? e.details : JSON.stringify(e.details, null, 2)).slice(0, 1500);
  return h("div", { class: "err" }, h("b", {}, x.title), x.fix ? h("div", { class: "err-fix", style: "white-space:pre-line" }, "→ " + x.fix) : "",
    raw ? h("details", {}, h("summary", {}, "Technical details"), h("pre", {}, raw)) : "");
}

// ------------------------------------------------------------------ monitor

const ago = (iso) => {
  const s = (Date.now() - Date.parse(iso.endsWith("Z") || /[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z")) / 1000;
  if (!isFinite(s)) return iso;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return `${Math.floor(s / 86400)} d ago`;
};
const pct = (a, b) => (b ? Math.round((100 * a) / b) + "%" : "–");
const money = (v) => "$" + (v || 0).toFixed(v >= 1 ? 2 : 4);

function runIcon(r) {
  if ((r.flags || []).includes("escalated")) return ["🙋", "Handed over to a human"];
  if (r.feedback && r.feedback.rating === "down") return ["👎", "Rated not helpful"];
  if (r.status === "failed") return ["⚠", "Failed"];
  return ["💬", "Answered"];
}

async function viewMonitor() {
  const q = new URLSearchParams((location.hash.split("?")[1]) || "");
  const tab = q.get("tab") || "inbox";
  const tabs = [["inbox", "📥 Inbox"], ["runs", "🔎 Conversations"], ["dash", "📊 Dashboard"]];
  const { agents } = await api("GET", "/agents");
  const body = await ({ inbox: monitorInbox, runs: monitorRuns, dash: monitorDash }[tab] || monitorInbox)(agents, q);
  return h("div", {},
    h("h1", {}, "Monitor"),
    h("p", { class: "muted" }, "What live agents are doing: hand-overs and unhappy users to follow up, every conversation turn with how it was answered, and health per agent. Preview and test runs are not included."),
    h("div", { class: "tabs" }, tabs.map(([k, l]) => h("a", { href: "#/monitor?tab=" + k, class: k === tab ? "active" : "" }, l))),
    body);
}

function agentFilter(agents, value, onChange) {
  return select([["", "All agents"], ...agents.map((a) => [a.name, a.display_name || a.name])], value, onChange);
}

function setQuery(params) {
  const q = new URLSearchParams((location.hash.split("?")[1]) || "");
  Object.entries(params).forEach(([k, v]) => (v ? q.set(k, v) : q.delete(k)));
  location.hash = "#/monitor?" + q.toString();
}

// ---- inbox

async function monitorInbox(agents, q) {
  const show = q.get("show") || "open", agent = q.get("agent") || "";
  const params = new URLSearchParams({ review: show === "resolved" ? "resolved" : "open", limit: "100" });
  if (agent) params.set("agent", agent);
  let { runs } = await api("GET", "/ops/runs?" + params);
  if (show === "mine") runs = runs.filter((r) => r.assignee === state.me.principal);
  const list = h("div", { class: "inbox" });
  const render = () => list.replaceChildren(...(runs.length ? runs.map((r) => inboxItem(r, () => { runs = runs.filter((x) => x !== r); render(); })) :
    [h("div", { class: "empty-state" }, show === "resolved" ? "Nothing resolved yet." : "🎉 Nothing to follow up. Hand-overs and 👎 answers from the chat app appear here.")]));
  render();
  return h("div", {},
    h("div", { class: "filters" },
      segmented([["open", "Open"], ["mine", "Assigned to me"], ["resolved", "Resolved"]], show, (v) => setQuery({ show: v })),
      agentFilter(agents, agent, (v) => setQuery({ agent: v })),
      h("span", { class: "muted small" }, `${runs.length} item(s)`)),
    list);
}

function inboxItem(r, onGone) {
  const [icon, why] = runIcon(r);
  const fb = r.feedback;
  const actions = h("div", { class: "row-btns" });
  const item = h("div", { class: "inbox-item" },
    h("div", { class: "ii-icon", title: why }, icon),
    h("div", { class: "ii-body" },
      h("div", { class: "ii-head" }, h("b", {}, r.agent), r.context_id ? badge(r.context_id) : "", h("span", { class: "muted small" }, `${why} · ${r.on_behalf_of} · ${ago(r.created_at)}`),
        r.assignee ? badge("👤 " + r.assignee, "") : ""),
      h("div", { class: "ii-q" }, "“" + (r.input_text || "") + "”"),
      r.output_text ? h("div", { class: "ii-a muted" }, r.output_text) : "",
      fb && fb.comment ? h("div", { class: "ii-fb" }, "User comment: ", h("i", {}, fb.comment)) : "",
      r.review_note ? h("div", { class: "ii-fb" }, "Note: ", r.review_note, h("span", { class: "muted small" }, ` — ${r.reviewed_by}`)) : "",
      actions));
  const review = (payload, msg) => act(async () => {
    const x = await api("POST", `/ops/runs/${r.run_id}/review`, payload);
    Object.assign(r, x); toast(msg);
    if (payload.status) onGone(); else item.replaceWith(inboxItem(r, onGone));
  });
  actions.append(h("button", { class: "small-btn", onclick: () => openRun(r.run_id) }, "Open conversation"));
  if (r.review_status === "open") {
    if (r.assignee !== state.me.principal) actions.append(h("button", { class: "small-btn", onclick: () => review({ assignee: "me" }, "Assigned to you") }, "Assign to me"));
    actions.append(h("button", { class: "small-btn", onclick: () => testFromRun(r) }, "＋ Turn into a test"));
    actions.append(h("button", { class: "small-btn primary", onclick: () => {
      const note = h("textarea", { rows: 3, placeholder: "e.g. Called the customer back; added the VPN article to Facts & FAQ" });
      const close = modal("Resolve", h("p", { class: "muted" }, "What was done? The note is kept with the conversation and in the audit log."), note,
        h("div", { class: "row-btns" }, h("button", { onclick: () => close() }, "Cancel"),
          h("button", { class: "primary", onclick: () => { close(); review({ status: "resolved", note: note.value }, "Resolved"); } }, "Resolve")));
    } }, "✓ Resolve"));
  } else {
    actions.append(h("button", { class: "small-btn", onclick: () => review({ status: "open" }, "Re-opened") }, "Re-open"));
  }
  return item;
}

// A 👎 or hand-over becomes a regression test: "when someone says X, the answer must …".
function testFromRun(r) {
  const fb = (r.feedback && r.feedback.comment) || "";
  const expect = h("textarea", { rows: 3, placeholder: "What a good answer does, e.g. Explains how to restart the VPN client and links the VPN guide" });
  expect.value = fb ? `A good answer addresses the user's complaint: ${fb}` : "";
  const must = h("input", { placeholder: "comma separated, e.g. VPN client, restart" });
  const handover = h("input", { type: "checkbox", checked: (r.flags || []).includes("escalated") });
  const close = modal("Turn into a test",
    h("p", { class: "muted" }, `When a user in “${r.context_id || "default"}” says “${(r.input_text || "").slice(0, 120)}”, the agent must…`),
    field("give an answer that (AI judge)", expect), field("mention", must),
    h("label", { class: "toggle" }, handover, h("span", {}, "hand the conversation over to a human")),
    h("p", { class: "muted small" }, "Saved to the “from-monitor” suite of the agent; it runs in Release → ① Test before the next go-live."),
    h("div", { class: "row-btns" }, h("button", { onclick: () => close() }, "Cancel"),
      h("button", { class: "primary", onclick: () => act(async () => {
        const assertions = [];
        if (expect.value.trim()) assertions.push({ type: "llm_judge", threshold: 0.7, rubric: expect.value.trim() });
        must.value.split(",").map((x) => x.trim()).filter(Boolean).forEach((v) => assertions.push({ type: "contains", path: "output", value: v }));
        if (handover.checked) assertions.push({ type: "contains", path: "flags", value: "escalated" });
        if (!assertions.length) throw new Error("Describe at least one expectation.");
        const res = await api("POST", `/agents/${encodeURIComponent(r.agent)}/evals/cases`,
          { suite: "from-monitor", case: { context: r.context_id || undefined, input: { message: r.input_text }, assertions } });
        close(); toast(`Saved as test ${res.case_id} in “${res.suite}” (${res.cases} test(s))`);
      }) }, "Save test")));
}

// ---- run detail

async function openRun(id) {
  const close = modal("Conversation turn", h("p", { class: "muted" }, "Loading…"));
  let d;
  try { d = await api("GET", "/ops/runs/" + id); } catch (e) { close(); fail(e); return; }
  close();
  const msg = d.input && typeof d.input === "object" ? (d.input.message ?? JSON.stringify(d.input, null, 2)) : String(d.input ?? "");
  const out = typeof d.output === "string" ? d.output : d.output == null ? "" : JSON.stringify(d.output, null, 2);
  const kv = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
  const review = h("div", {});
  const renderReview = () => {
    review.replaceChildren();
    if (!d.review_status && !d.can_review) return;
    const note = h("input", { placeholder: "note", value: d.review_note || "" });
    review.append(h("h3", {}, "Follow-up"),
      h("p", {}, d.review_status === "open" ? badge("open in inbox", "warn") : d.review_status === "resolved" ? badge("resolved", "ok") : badge("not in inbox"),
        d.assignee ? ` assigned to ${d.assignee}` : "",
        d.reviewed_by ? h("span", { class: "muted small" }, ` · last change by ${d.reviewed_by}, ${ago(d.reviewed_at)}`) : ""),
      d.can_review ? h("div", { class: "row-btns" }, note,
        d.assignee !== state.me.principal ? h("button", { class: "small-btn", onclick: () => upd({ assignee: "me" }) }, "Assign to me") : "",
        d.review_status !== "open" ? h("button", { class: "small-btn", onclick: () => upd({ status: "open", note: note.value }) }, "Add to inbox") :
          h("button", { class: "small-btn primary", onclick: () => upd({ status: "resolved", note: note.value }) }, "✓ Resolve")) : "");
  };
  const upd = (p) => act(async () => { Object.assign(d, await api("POST", `/ops/runs/${id}/review`, p)); renderReview(); toast("Saved"); });
  renderReview();
  modal(`${d.agent} · ${ago(d.created_at)}`,
    h("div", { class: "run-detail" },
      h("dl", { class: "kv" },
        ...kv("Who", d.on_behalf_of + (d.principal !== d.on_behalf_of ? ` (via ${d.principal})` : "")),
        ...kv("Agent", `${d.agent} v${d.version}${d.alias ? " @" + d.alias : ""}${d.context_id ? " · context " + d.context_id : ""}`),
        ...kv("Outcome", h("span", {}, statusBadge(d.status), " ", ...(d.flags || []).map((f) => badge(humanFlag(f), "warn")))),
        ...kv("Cost & time", `${money(d.cost_usd)} · ${d.latency_ms} ms · ${d.input_tokens}/${d.output_tokens} tokens`),
        ...kv("Feedback", d.feedback ? `${d.feedback.rating === "up" ? "👍" : "👎"} ${d.feedback.comment || ""}` : "–"),
        ...kv("Trace", h("code", { class: "small" }, d.trace_id))),
      h("div", { class: "bubble user" }, msg),
      d.status === "failed" && d.error ? errorView({ code: d.error.code, message: d.error.message }) : h("div", { class: "bubble bot" }, mdLite(out || "(no answer)")),
      timelineView({ trace: { steps: d.steps || [] }, latency_ms: d.latency_ms, cost: { usd: d.cost_usd } }),
      (d.sources || []).length ? h("div", { class: "small muted" }, "Sources: ", ...(d.sources || []).map((s, i) => h("span", {}, `[${i + 1}] ${s.title || s.uri} `))) : "",
      (d.related || []).length ? h("div", {}, h("h3", {}, "Other agents in this conversation turn"),
        h("ul", {}, d.related.map((x) => h("li", {}, h("a", { href: "javascript:void 0", onclick: () => { document.querySelectorAll(".modal-overlay").forEach((o) => o.remove()); openRun(x.run_id); } },
          `${x.agent} v${x.version}`), " ", statusBadge(x.status), h("span", { class: "muted small" }, ` depth ${x.depth} · ${money(x.cost_usd)}`))))) : "",
      review,
      h("div", { class: "row-btns" }, h("button", { class: "small-btn", onclick: () => testFromRun({ ...d, input_text: msg }) }, "＋ Turn into a test"),
        h("a", { class: "small", href: `#/agents/${encodeURIComponent(d.agent)}/studio${d.context_id ? "?ctx=" + d.context_id : ""}`,
          onclick: () => document.querySelectorAll(".modal-overlay").forEach((o) => o.remove()) }, "Open in studio →"))));
  const m = document.querySelector(".modal:last-of-type"); if (m) m.classList.add("wide");
}

// ---- runs explorer

async function monitorRuns(agents, q) {
  const f = { agent: q.get("agent") || "", status: q.get("status") || "", flag: q.get("flag") || "", feedback: q.get("feedback") || "", q: q.get("q") || "",
    include_tests: q.get("include_tests") || "" };
  const params = new URLSearchParams({ limit: "100" });
  Object.entries(f).forEach(([k, v]) => v && params.set(k, v));
  const { runs } = await api("GET", "/ops/runs?" + params);
  const search = h("input", { type: "search", placeholder: "Search questions, answers, run or trace id…", value: f.q });
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") setQuery({ q: search.value }); });
  const tests = h("input", { type: "checkbox", checked: !!f.include_tests });
  tests.addEventListener("change", () => setQuery({ include_tests: tests.checked ? "true" : "" }));
  return h("div", {},
    h("div", { class: "filters" }, search,
      agentFilter(agents, f.agent, (v) => setQuery({ agent: v })),
      select([["", "Any outcome"], ["ok", "Answered"], ["partial", "Partial"], ["failed", "Failed"], ["needs_input", "Waiting for input"]], f.status, (v) => setQuery({ status: v })),
      select([["", "Any handling"], ["escalated", "Handed over"], ["responded:topic_policy", "Refused: off-topic"], ["responded:pii", "Personal data rule"]], f.flag, (v) => setQuery({ flag: v })),
      select([["", "Any feedback"], ["up", "👍"], ["down", "👎"]], f.feedback, (v) => setQuery({ feedback: v })),
      h("label", { class: "toggle small" }, tests, h("span", {}, "include previews & tests"))),
    runs.length ? h("table", { class: "runs-table" },
      h("thead", {}, h("tr", {}, ...["", "When", "Agent", "Who", "Question", "Outcome", "Cost", "Time"].map((x) => h("th", {}, x)))),
      h("tbody", {}, runs.map((r) => {
        const [icon, why] = runIcon(r);
        return h("tr", { class: "clickable", onclick: () => openRun(r.run_id), title: "Open the conversation turn" },
          h("td", { title: why }, icon), h("td", { class: "nowrap" }, ago(r.created_at)),
          h("td", {}, r.agent, r.context_id ? h("div", { class: "muted small" }, r.context_id) : ""),
          h("td", { class: "small" }, r.on_behalf_of), h("td", { class: "q-cell" }, r.input_text),
          h("td", {}, statusBadge(r.status), " ", ...(r.flags || []).map((x) => badge(humanFlag(x), "warn")), r.feedback ? (r.feedback.rating === "up" ? " 👍" : " 👎") : ""),
          h("td", { class: "num" }, money(r.cost_usd)), h("td", { class: "num" }, `${r.latency_ms} ms`));
      }))) : h("div", { class: "empty-state" }, "No conversations match. Chat with a published agent in the chat app, then come back."));
}

// ---- dashboard

async function monitorDash(agents, q) {
  const days = q.get("days") || "14", agent = q.get("agent") || "";
  const params = new URLSearchParams({ days }); if (agent) params.set("agent", agent);
  const s = await api("GET", "/ops/stats?" + params);
  const tot = s.agents.reduce((t, a) => { ["runs", "ok", "failed", "escalated", "refused", "up", "down", "open"].forEach((k) => (t[k] += a[k] || 0)); t.cost += a.cost_usd; return t; },
    { runs: 0, ok: 0, failed: 0, escalated: 0, refused: 0, up: 0, down: 0, open: 0, cost: 0 });
  const tile = (label, value, sub, href) => h(href ? "a" : "div", { class: "kpi", ...(href ? { href } : {}) }, h("div", { class: "kpi-v" }, value), h("div", { class: "kpi-l" }, label), sub ? h("div", { class: "muted small" }, sub) : "");
  return h("div", {},
    h("div", { class: "filters" },
      segmented([["7", "7 days"], ["14", "14 days"], ["30", "30 days"]], days, (v) => setQuery({ days: v })),
      agentFilter(agents, agent, (v) => setQuery({ agent: v }))),
    h("div", { class: "kpis" },
      tile("conversation turns", tot.runs, `${s.agents.length} agent(s)`),
      tile("answered", pct(tot.ok, tot.runs), `${tot.failed} failed`),
      tile("handed over", tot.escalated, `${tot.refused} refused by rule`),
      tile("helpful", pct(tot.up, tot.up + tot.down), `${tot.up} 👍 · ${tot.down} 👎`),
      tile("to follow up", tot.open, "open in inbox", "#/monitor?tab=inbox"),
      tile("spend", money(tot.cost), `last ${s.days} days`)),
    s.agents.length ? h("div", { class: "dash-grid" }, s.agents.map((a) => agentHealth(a))) :
      h("div", { class: "empty-state" }, "No live traffic in this period yet."));
}

function agentHealth(a) {
  const ctx = Object.entries(a.contexts || {}).sort((x, y) => y[1] - x[1]);
  return h("section", { class: "panel health" },
    h("div", { class: "health-head" }, h("h3", {}, h("a", { href: `#/agents/${encodeURIComponent(a.agent)}` }, a.agent)), a.bu ? badge(a.bu) : "",
      a.open ? h("a", { class: "badge warn", href: `#/monitor?tab=inbox&agent=${encodeURIComponent(a.agent)}` }, `${a.open} to follow up`) : ""),
    dayBars(a.days),
    h("table", { class: "mini" }, h("tbody", {},
      h("tr", {}, h("td", {}, "Turns"), h("td", { class: "num" }, a.runs), h("td", {}, "Answered"), h("td", { class: "num" }, pct(a.ok, a.runs))),
      h("tr", {}, h("td", {}, "Failed"), h("td", { class: "num" }, a.failed), h("td", {}, "Handed over"), h("td", { class: "num" }, a.escalated)),
      h("tr", {}, h("td", {}, "Refused by rule"), h("td", { class: "num" }, a.refused), h("td", {}, "Helpful"), h("td", { class: "num" }, `${pct(a.up, a.up + a.down)} (${a.up}/${a.up + a.down})`)),
      h("tr", {}, h("td", {}, "Response time"), h("td", { class: "num" }, `${a.p50_ms} ms`), h("td", {}, "Slowest 5%"), h("td", { class: "num" }, `${a.p95_ms} ms`)),
      h("tr", {}, h("td", {}, "Spend"), h("td", { class: "num" }, money(a.cost_usd)), h("td", {}, "Per turn"), h("td", { class: "num" }, money(a.runs ? a.cost_usd / a.runs : 0))))),
    ctx.length > 1 ? h("p", { class: "small muted" }, "By context: " + ctx.map(([k, v]) => `${k} ${pct(v, a.runs)}`).join(" · ")) : "",
    h("a", { class: "small", href: `#/monitor?tab=runs&agent=${encodeURIComponent(a.agent)}` }, "See conversations →"));
}

// Turns per day: answered (accent) with failed stacked on top (status red), 2px gap, rounded data end.
function dayBars(days) {
  const W = 320, H = 70, pad = 2, n = days.length, bw = Math.max(3, (W - pad * (n - 1)) / n);
  const max = Math.max(1, ...days.map((d) => d.runs));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H + 14}`); svg.setAttribute("class", "daybars"); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Conversation turns per day: " + days.map((d) => `${d.day} ${d.runs}`).join(", "));
  const tip = h("div", { class: "chart-tip", hidden: true });
  const mk = (tag, attrs) => { const e = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); svg.append(e); return e; };
  mk("line", { x1: 0, x2: W, y1: H + 0.5, y2: H + 0.5, class: "axis" });
  const barPath = (x, y, w, hgt, round) => {
    const r = round ? Math.min(4, w / 2, hgt) : 0;
    return `M${x},${y + hgt} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + hgt} Z`;
  };
  days.forEach((d, i) => {
    const x = i * (bw + pad), ok = d.runs - d.failed;
    const hOk = (ok / max) * (H - 4), hF = (d.failed / max) * (H - 4);
    if (ok > 0) mk("path", { d: barPath(x, H - hOk, bw, hOk, d.failed === 0), class: "bar-ok" });
    if (d.failed > 0) mk("path", { d: barPath(x, H - hOk - hF - (ok > 0 ? 2 : 0), bw, hF, true), class: "bar-fail" });
    const hit = mk("rect", { x, y: 0, width: bw + pad, height: H, fill: "transparent" });
    hit.addEventListener("mouseenter", () => { tip.hidden = false; tip.textContent = `${d.day}: ${d.runs} turn(s), ${d.failed} failed, ${money(d.cost_usd)}`; });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
  [[0, days[0].day.slice(5)], [W, days[n - 1].day.slice(5)]].forEach(([x, t], i) =>
    mk("text", { x, y: H + 12, class: "tick", "text-anchor": i ? "end" : "start" }).textContent = t);
  const legend = h("div", { class: "legend small muted" }, h("span", { class: "sw sw-ok" }), "answered ", h("span", { class: "sw sw-fail" }), "failed",
    h("span", { class: "muted" }, ` · peak ${max}/day`));
  return h("div", { class: "chart" }, svg, tip, legend);
}

window.explainError = explainError; window.errorView = errorView; window.viewMonitor = viewMonitor; window.openRun = openRun;
