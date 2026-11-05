// DOM helper + JSON-schema form generator. Plugin params (patterns, guardrails, tools,
// knowledge, assertions) are rendered from the schemas served by GET /api/v1/plugins, so a
// new plugin becomes authorable without console changes.
"use strict";

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const LONG_TEXT_KEYS = /^(system|instructions|text|sql|rubric|map_prompt|reduce_prompt|review_prompt|description)$/;

function jsonArea(value, onChange, rows) {
  const ta = h("textarea", { rows: rows || 5 });
  ta.value = value === undefined ? "" : JSON.stringify(value, null, 2);
  ta.addEventListener("change", () => {
    if (!ta.value.trim()) { ta.style.borderColor = ""; onChange(undefined); return; }
    try { const v = JSON.parse(ta.value); ta.style.borderColor = ""; onChange(v); }
    catch (e) { ta.style.borderColor = "var(--bad)"; }
  });
  return ta;
}

// Renders a form for `schema`; calls onChange(newValue) whenever a field changes.
function schemaForm(schema, value, onChange, key) {
  schema = schema || {};
  const t = schema.type;
  if (schema.enum) {
    const sel = h("select", {}, h("option", { value: "" }, "—"),
      schema.enum.map((o) => h("option", { value: o }, String(o))));
    sel.value = value === undefined || value === null ? "" : value;
    sel.addEventListener("change", () => onChange(sel.value === "" ? undefined : sel.value));
    return sel;
  }
  if (t === "boolean") {
    const cb = h("input", { type: "checkbox", checked: value === undefined ? schema.default : value });
    cb.addEventListener("change", () => onChange(cb.checked));
    return cb;
  }
  if (t === "integer" || t === "number") {
    const inp = h("input", { type: "number", step: t === "integer" ? "1" : "any", placeholder: schema.default ?? "" });
    if (value !== undefined) inp.value = value;
    inp.addEventListener("change", () => onChange(inp.value === "" ? undefined : Number(inp.value)));
    return inp;
  }
  if (t === "string") {
    const long = LONG_TEXT_KEYS.test(key || "");
    const inp = long ? h("textarea", { class: "prose", rows: 4 }) : h("input", { type: "text" });
    inp.placeholder = schema.default ?? schema.description ?? "";
    if (value !== undefined) inp.value = value;
    inp.addEventListener("change", () => onChange(inp.value === "" ? undefined : inp.value));
    return inp;
  }
  if (t === "array" && schema.items && schema.items.enum) {
    const cur = new Set(value || []);
    return h("div", { class: "row" }, schema.items.enum.map((o) => {
      const cb = h("input", { type: "checkbox", checked: cur.has(o) });
      cb.addEventListener("change", () => { cb.checked ? cur.add(o) : cur.delete(o); onChange([...cur]); });
      return h("label", {}, cb, " ", o);
    }));
  }
  if (t === "array" && schema.items && schema.items.type === "string") {
    const inp = h("input", { type: "text", placeholder: "comma separated" });
    inp.value = (value || []).join(", ");
    inp.addEventListener("change", () => {
      const v = inp.value.split(",").map((s) => s.trim()).filter(Boolean);
      onChange(v.length ? v : undefined);
    });
    return inp;
  }
  if (t === "object" && schema.properties && Object.keys(schema.properties).length) {
    const obj = Object.assign({}, value || {});
    const req = new Set(schema.required || []);
    const box = h("div", {});
    for (const [k, sub] of Object.entries(schema.properties)) {
      const field = schemaForm(sub, obj[k], (v) => {
        if (v === undefined) delete obj[k]; else obj[k] = v;
        onChange(Object.assign({}, obj));
      }, k);
      box.append(h("label", { title: sub.description || "" }, k, req.has(k) ? " *" : "",
        sub.description ? h("span", { class: "muted" }, " — ", sub.description) : null), field);
    }
    return box;
  }
  if (t === "object" && schema.properties && !Object.keys(schema.properties).length) {
    return h("p", { class: "muted" }, "No parameters.");
  }
  return jsonArea(value, onChange); // free-form object / anything else
}

window.h = h;
window.schemaForm = schemaForm;
window.jsonArea = jsonArea;
