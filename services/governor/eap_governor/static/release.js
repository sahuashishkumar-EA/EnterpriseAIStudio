// Release as one guided flow: ① Test → ② Review → ③ Go live → ④ Live & history.
// Plus plain-language helpers (flags, blockers) and a draft-vs-live diff.
"use strict";

const FLAG_LABELS = {
  "escalated": "Handed over to a human",
  "responded:topic_policy": "Refused: off-topic",
  "responded:escalation": "Handed over to a human",
  "responded:pii_redaction": "Asked not to share personal data",
};
function humanFlag(f) { return FLAG_LABELS[f] || String(f).replace(/^responded:/, "Answered by rule: "); }

// Release-gate messages in plain language, each with what to do about it.
function humanBlocker(b, relaxed) {
  let m;
  if ((m = b.match(/^eval: suite (.+) has no passing run/))) return [`Tests in “${m[1]}” have not passed on the latest changes.`, "Run the tests in step ①."];
  if (/^eval: no required eval suite/.test(b)) return ["There are no tests yet.", "Add a test suite (or use “Save as test” in the live preview)."];
  if ((m = b.match(/^approval: rejected by (.+)/))) return [`Rejected by ${m[1]}.`, "Address the feedback, save, and ask for a new review."];
  if ((m = b.match(/^approval: (\d+)\/(\d+) valid approvals/))) return [`Needs approval (${m[1]} of ${m[2]}).`,
    relaxed ? "Approve it in step ② (dev mode allows approving your own changes)." : "Someone who did not edit this version must approve it in step ②."];
  if (/^semver:/.test(b)) return ["This change breaks how other systems call the agent.", "Publish it as a new major version (choose “major” in step ③)."];
  if (/^draft is identical/.test(b)) return ["Nothing has changed since the last version.", "Make a change first."];
  if ((m = b.match(/^validation: (\d+)/))) return [`${m[1]} problem(s) in the setup.`, "Open Technical setup → Validate to see them."];
  return [b, ""];
}

// ------------------------------------------------------------------ diff

function flatten(v, prefix, out) {
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, x] of Object.entries(v)) flatten(x, prefix ? `${prefix}.${k}` : k, out);
  } else out[prefix] = v;
  return out;
}

const SECTION_NAMES = { prompt: "System-level context", model: "Model", pattern: "Orchestration", bindings: "Tools & knowledge",
  guardrails: "Technical guardrails", limits: "Limits", output: "Output format", contract: "Interface (contract)",
  exposure: "Channels", description: "Description", data_classification: "Data classification", tags: "Tags", default_context: "Default context" };

const FIELD_LABELS = { "behavior.tone": "Tone", "behavior.verbosity": "Answer length", "behavior.format": "Format",
  "behavior.language": "Language", "behavior.emoji": "Emoji", "guardrails.refusal_message": "Refusal reply",
  "guardrails.blocked_topics": "Refused topics", "guardrails.allowed_topics": "Stick-to topics",
  "guardrails.escalation.triggers": "Hand-over triggers", "guardrails.escalation.message": "Hand-over reply",
  "guardrails.personal_data": "Personal data", "guardrails.disclaimer": "Disclaimer", "always": "Always", "never": "Never",
  "notes": "Facts & FAQ", "web_sources": "Web pages", "examples": "Examples", "starters": "Conversation starters",
  "role": "Role", "goal": "Goal", "audience": "Audience", "description": "Description", "name": "Name",
  "system": "System prompt", "instructions": "Instructions", "catalog_id": "Model", "fallbacks": "Fallback models",
  "type": "Pattern", "tools": "Tools", "agents": "Agents", "knowledge": "Knowledge" };
const fieldLabel = (k) => FIELD_LABELS[k] || FIELD_LABELS[k.split(".").pop()] || k;

function diffDefinitions(prev, next) {
  const changes = [];
  if (!prev) return changes;
  const show = (v) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));
  const cmp = (a, b, section, label) => {
    const fa = flatten(a, "", {}), fb = flatten(b, "", {});
    for (const k of new Set([...Object.keys(fa), ...Object.keys(fb)])) {
      if (JSON.stringify(fa[k]) !== JSON.stringify(fb[k])) changes.push({ section, label, field: k || "(value)", before: show(fa[k]), after: show(fb[k]) });
    }
  };
  const skip = new Set(["contexts", "name", "schema_version", "owner_bu"]);
  for (const k of new Set([...Object.keys(prev), ...Object.keys(next)])) {
    if (skip.has(k)) continue;
    cmp(prev[k], next[k], SECTION_NAMES[k] || k, "");
  }
  const pc = Object.fromEntries((prev.contexts || []).map((c) => [c.id, c]));
  const nc = Object.fromEntries((next.contexts || []).map((c) => [c.id, c]));
  for (const id of new Set([...Object.keys(pc), ...Object.keys(nc)])) {
    const label = (nc[id] || pc[id]).name || id;
    if (!pc[id]) changes.push({ section: "Functional contexts", label, field: "", before: "—", after: "added" });
    else if (!nc[id]) changes.push({ section: "Functional contexts", label, field: "", before: "existed", after: "removed" });
    else cmp(pc[id], nc[id], "Functional contexts", label);
  }
  return changes;
}

function diffView(diff) {
  if (!diff.latest_version) return h("p", { class: "muted" }, "First version — everything is new.");
  const changes = diffDefinitions(diff.previous, diff.draft);
  if (!changes.length) return h("p", { class: "muted" }, `No changes since version ${diff.latest_version}.`);
  const groups = {};
  for (const c of changes) (groups[c.section + (c.label ? " · " + c.label : "")] ||= []).push(c);
  return h("div", { class: "diff" },
    h("p", { class: "muted small" }, `${changes.length} change(s) since version ${diff.latest_version}:`),
    diff.breaking_changes.length ? h("div", { class: "warn-text" }, "⚠ Breaking: " + diff.breaking_changes.join("; ")) : "",
    ...Object.entries(groups).map(([g, cs]) => h("details", { open: cs.length <= 6 }, h("summary", {}, h("b", {}, g), h("span", { class: "muted" }, ` (${cs.length})`)),
      h("table", { class: "diff-table" }, cs.map((c) => h("tr", {}, h("td", { class: "muted" }, fieldLabel(c.field)),
        h("td", { class: "diff-old" }, String(c.before).slice(0, 400)), h("td", { class: "diff-new" }, String(c.after).slice(0, 400))))))));
}

// ------------------------------------------------------------------ the flow

async function tabReleaseFlow(agent) {
  const name = agent.name;
  const [chk, diff, { suites }] = await Promise.all([
    api("GET", `/agents/${name}/release-check`), api("GET", `/agents/${name}/draft/diff`), api("GET", `/agents/${name}/evals/suites`)]);
  const technical = persona() === "technical";
  const prod = agent.aliases.find((a) => a.alias === "prod");
  const noChanges = chk.blockers.some((b) => /^draft is identical/.test(b));
  const testBlock = chk.blockers.filter((b) => /^eval:|^validation:/.test(b));
  const reviewBlock = chk.blockers.filter((b) => /^approval:|^semver:/.test(b));
  const testsOk = !testBlock.length, reviewOk = !reviewBlock.length;
  const stepState = [
    noChanges ? "done" : testsOk ? "done" : "todo",
    noChanges ? "done" : reviewOk ? "done" : testsOk ? "todo" : "wait",
    noChanges ? "done" : chk.ok ? "todo" : "wait",
    prod ? "done" : "wait",
  ];
  const labels = ["Test", "Review", "Go live", "Live & history"];
  const stepper = h("ol", { class: "stepper" }, labels.map((l, i) => h("li", { class: "st-" + stepState[i] },
    h("span", { class: "st-num" }, stepState[i] === "done" ? "✓" : String(i + 1)), h("span", {}, l))));
  const blockerList = (bs) => bs.length ? h("ul", { class: "blockers" }, bs.map((b) => { const [t, fix] = humanBlocker(b, chk.relaxed); return h("li", {}, t, fix ? h("div", { class: "muted small" }, fix) : ""); })) : "";

  // ① Test
  const testOut = h("div", {});
  const runTests = () => act(async () => {
    testOut.replaceChildren(h("p", { class: "muted" }, "Running tests on the latest changes…"));
    const r = await api("POST", `/agents/${name}/evals/run?wait=true`, { target: "draft" });
    toast(r.runs.map((x) => `${x.passed}/${x.total} passed`).join(", ")); route();
  });
  const ext = ((agent.draft.definition.hosting || {}).platform || "internal") !== "internal";
  const extName = ext ? PLATFORMS[agent.draft.definition.hosting.platform] : "";
  const step1 = h("section", { class: "step panel" }, h("h2", {}, "① Test the latest changes"),
    ext ? h("p", { class: "muted small" }, `Tests run against the real agent on ${extName} (its configured endpoint), not a simulation.`) : "",
    suites.length ? h("table", {}, chk.evals.map((e) => h("tr", {}, h("td", {}, e.suite), h("td", { class: "muted" }, e.required ? "required" : "optional"),
      h("td", { class: e.passed ? "pass" : "fail" }, e.run ? `${e.passed ? "✓" : "✗"} ${e.run.passed}/${e.run.total} passed` : "not run on these changes")))) :
      h("p", {}, "No tests yet. Add a suite below, or use “Save as test” on answers in the Functional studio's live preview."),
    blockerList(testBlock),
    h("div", { class: "row-btns" }, h("button", { class: "primary", disabled: !suites.length, onclick: runTests }, "Run tests")), testOut,
    h("details", {}, h("summary", {}, "Manage test suites and see detailed results"), await tabEvals(agent)));

  // ② Review
  const comment = h("input", { placeholder: "Review comment (optional)" });
  const decide = (decision) => act(async () => {
    await api("POST", `/agents/${name}/approvals`, { decision, comment: comment.value, content_hash: chk.content_hash });
    toast(decision === "approve" ? "Approved" : "Rejected"); route();
  });
  const step2 = h("section", { class: "step panel" }, h("h2", {}, "② Review what changed"),
    diffView(diff),
    h("h3", {}, `Approvals (${chk.approvals.valid.length} of ${chk.approvals.required})`),
    chk.approvals.all.length ? h("ul", { class: "plain" }, chk.approvals.all.map((a) => h("li", {}, a.decision === "approve" ? "✓ " : "✗ ", h("b", {}, a.approver), a.comment ? ` — ${a.comment}` : ""))) : "",
    chk.relaxed ? h("p", { class: "muted small" }, "Dev mode: you may approve your own changes.") :
      chk.approvals.separation_of_duties ? h("p", { class: "muted small" }, `People who edited this version cannot approve it (${chk.approvals.excluded.join(", ") || "—"}).`) : "",
    blockerList(reviewBlock),
    noChanges ? "" : h("div", { class: "row" }, field("Comment", comment), h("div", { style: "flex:0 0 auto; display:flex; gap:8px" },
      h("button", { class: "primary", onclick: () => decide("approve") }, "Approve"), h("button", { class: "danger", onclick: () => decide("reject") }, "Reject"))));

  // ③ Go live
  const changelog = h("input", { placeholder: "What changed, in one line" });
  const golive = h("input", { type: "checkbox", checked: true });
  const bump = select([["", `automatic (${chk.required_bump})`], ["patch", "patch"], ["minor", "minor"], ["major", "major"]], "", () => {});
  const publish = () => act(async () => {
    const r = await api("POST", `/agents/${name}/publish`, { bump: bump.value || undefined, changelog: changelog.value, alias: golive.checked ? "prod" : undefined });
    toast(golive.checked ? `Version ${r.version} is live` : `Version ${r.version} published (not live)`); route();
  });
  const step3 = h("section", { class: "step panel" }, h("h2", {}, "③ Go live"),
    ext ? h("p", { class: "muted small" }, `Going live publishes the new instructions and guardrails for ${extName}: its profile changes and the change webhook (if set) tells it to refresh. Guardrail checks apply immediately.`) : "",
    noChanges ? h("p", { class: "muted" }, prod ? `Nothing new — version ${prod.version} is live and matches your latest changes.` : "Nothing new to publish.") : h("div", {},
      h("p", {}, chk.ok ? h("span", { class: "pass" }, `✓ Ready: this becomes version ${chk.next_version}.`) : h("span", { class: "muted" }, `Finish steps ① and ② first. This will become version ${chk.next_version}.`)),
      h("div", { class: "row" }, field("Changelog", changelog), technical ? field("Version bump", bump) : "",
        field("Go live", h("label", { class: "toggle" }, golive, h("span", {}, "make it live for users (prod)"))),
        h("div", { style: "flex:0 0 auto" }, h("button", { class: "primary", disabled: !chk.ok, onclick: publish }, golive.checked ? "Publish & go live" : "Publish")))),
    state.settings.relaxed_release && !noChanges ? h("p", { class: "muted small" }, "Dev mode shortcut: ",
      h("a", { href: `#/agents/${name}/overview` }, "⚡ Quick publish"), " runs tests, approves and goes live in one step.") : "",
    technical ? h("p", { class: "muted small" }, "Draft fingerprint ", h("code", {}, chk.content_hash.slice(0, 19))) : "");

  // ④ Live & history
  const step4 = h("section", { class: "step panel" }, h("h2", {}, "④ Live & history"),
    h("p", {}, prod ? h("span", {}, "Live version: ", h("b", {}, prod.version), prod.canary_version ? ` (trying ${prod.canary_version} with ${prod.canary_weight}% of users)` : "") : "Not live yet."),
    h("details", { open: !!prod }, h("summary", {}, "Versions, rollback and gradual rollout"), await tabVersions(agent)));

  return h("div", { class: "release-flow" }, stepper, step1, step2, step3, step4);
}

window.tabReleaseFlow = tabReleaseFlow;
window.humanFlag = humanFlag;
