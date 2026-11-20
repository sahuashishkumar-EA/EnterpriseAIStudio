// People & business units: who belongs to which BU and what they may do there.
// Platform admins manage everything; BU admins manage the people of their own BU(s).
"use strict";

async function viewPeople() {
  const [bu, people] = await Promise.all([api("GET", "/business-units"),
    api("GET", "/users").catch((e) => (e.status === 403 ? { users: null } : Promise.reject(e)))]);
  const roleLabels = Object.fromEntries(bu.roles.map((r) => [r.id, r.label]));
  const manageable = bu.business_units.filter((b) => b.can_manage);
  const busById = Object.fromEntries(bu.business_units.map((b) => [b.id, b]));

  const buCard = (b) => h("div", { class: "bu-card" + (b.status === "archived" ? " archived" : "") },
    h("div", { class: "bu-head" }, h("b", {}, b.name), h("code", { class: "small" }, b.id), b.status === "archived" ? badge("archived", "warn") : ""),
    b.description ? h("p", { class: "muted small" }, b.description) : "",
    h("div", { class: "small muted" }, `${b.members} people · ${b.agents} agent(s)`),
    b.can_manage ? h("div", { class: "row-btns" },
      h("button", { class: "small-btn", onclick: () => editBu(b) }, "Edit"),
      h("a", { class: "small", href: `#/people?bu=${b.id}` }, "People →")) : "");

  const editBu = (b) => {
    const name = h("input", { value: b.name }), desc = h("textarea", { rows: 2 }); desc.value = b.description || "";
    const welcome = h("textarea", { rows: 2, placeholder: "Shown in the chat app after people pick this BU" }); welcome.value = (b.settings || {}).chat_welcome || "";
    const close = modal(`Business unit · ${b.id}`, field("Name", name), field("Description", desc), field("Chat welcome text", welcome),
      h("div", { class: "row-btns" }, h("button", { onclick: () => close() }, "Cancel"),
        h("button", { class: "primary", onclick: () => act(async () => {
          await api("PUT", `/business-units/${b.id}`, { name: name.value, description: desc.value, settings: { chat_welcome: welcome.value } });
          close(); toast("Saved"); route();
        }) }, "Save")));
  };

  const newBu = () => {
    const id = h("input", { placeholder: "e.g. retail" }), name = h("input", { placeholder: "e.g. Retail Banking" }), desc = h("input", {});
    const close = modal("New business unit", field("Id (lowercase, used in policies and roles)", id), field("Name", name), field("Description", desc),
      h("div", { class: "row-btns" }, h("button", { onclick: () => close() }, "Cancel"),
        h("button", { class: "primary", onclick: () => act(async () => {
          await api("POST", "/business-units", { id: id.value.trim(), name: name.value.trim(), description: desc.value });
          close(); toast("Business unit created"); route();
        }) }, "Create")));
  };

  // edit one person: details + roles per BU you manage
  const editPerson = (u) => {
    u = u || { email: "", display_name: "", status: "active", default_bu: "", memberships: {}, attributes: {} };
    const isNew = !u.email;
    const email = h("input", { value: u.email, placeholder: "name@company.com", disabled: !isNew });
    const name = h("input", { value: u.display_name || "" });
    const lang = h("input", { value: (u.attributes || {}).language || "", placeholder: "e.g. nl, en (optional)" });
    const defBu = select([["", "—"], ...manageable.map((b) => [b.id, b.name])], u.default_bu || "", () => {});
    const status = select([["active", "Active"], ["disabled", "Disabled (no access anywhere)"]], u.status, () => {});
    const roleBoxes = {};
    const grid = h("table", { class: "role-grid" },
      h("tr", {}, h("th", {}, "Business unit"), ...bu.roles.map((r) => h("th", { title: r.label }, r.id.replace("_", " ")))),
      manageable.map((b) => h("tr", {}, h("td", {}, b.name),
        ...bu.roles.map((r) => {
          const c = h("input", { type: "checkbox", checked: (u.memberships[b.id] || []).includes(r.id), title: `${b.name}: ${r.label}` });
          (roleBoxes[b.id] = roleBoxes[b.id] || {})[r.id] = c;
          return h("td", { class: "c" }, c);
        }))));
    const close = modal(isNew ? "Add a person" : `Person · ${u.email}`,
      h("div", { class: "row" }, field("Email", email), field("Name", name)),
      h("div", { class: "row" }, field("Default business unit (chat)", defBu), field("Language", lang),
        bu.can_create ? field("Status", status) : ""),
      h("h3", {}, "Roles per business unit"),
      h("p", { class: "muted small" }, "Any role makes the person a member of the BU (they can use its agents in the chat app). ",
        "Member only = chat user without console access."),
      grid,
      h("details", { class: "small" }, h("summary", {}, "What do the roles mean?"),
        h("ul", {}, bu.roles.map((r) => h("li", {}, h("b", {}, r.id), " — ", r.label)))),
      h("div", { class: "row-btns" }, h("button", { onclick: () => close() }, "Cancel"),
        h("button", { class: "primary", onclick: () => act(async () => {
          const em = email.value.trim().toLowerCase();
          const payload = { display_name: name.value, default_bu: defBu.value || null, attributes: { ...(u.attributes || {}), language: lang.value.trim() || undefined } };
          if (bu.can_create) payload.status = status.value;
          await api("PUT", `/users/${encodeURIComponent(em)}`, payload);
          for (const b of manageable) {
            const want = bu.roles.filter((r) => roleBoxes[b.id][r.id].checked).map((r) => r.id);
            const had = (u.memberships[b.id] || []).slice().sort();
            if (JSON.stringify(want.slice().sort()) !== JSON.stringify(had)) await api("PUT", `/users/${encodeURIComponent(em)}/memberships/${b.id}`, { roles: want });
          }
          close(); toast("Saved"); route();
        }) }, "Save")));
    const m = document.querySelector(".modal:last-of-type"); if (m) m.classList.add("wide");
  };

  const q = new URLSearchParams((location.hash.split("?")[1]) || "");
  const onlyBu = q.get("bu") || "";
  const search = h("input", { type: "search", placeholder: "Search people…" });
  const tbody = h("tbody", {});
  const renderPeople = () => {
    const s = search.value.toLowerCase();
    const rows = (people.users || []).filter((u) => (!onlyBu || u.memberships[onlyBu]) && (!s || (u.email + " " + u.display_name).toLowerCase().includes(s)));
    tbody.replaceChildren(...rows.map((u) => h("tr", { class: u.status === "disabled" ? "muted" : "" },
      h("td", {}, h("b", {}, u.display_name || u.email.split("@")[0]), h("div", { class: "small muted" }, u.email)),
      h("td", {}, Object.entries(u.memberships).map(([b, rs]) => h("span", { class: "chip", title: rs.map((r) => roleLabels[r] || r).join("\n") },
        (busById[b] || { name: b === "*" ? "All BUs" : b }).name, ": ", rs.join(", ")))),
      h("td", {}, u.default_bu ? (busById[u.default_bu] || { name: u.default_bu }).name : "–"),
      h("td", {}, u.status === "disabled" ? badge("disabled", "bad") : badge("active", "ok"), u.last_seen_at ? h("div", { class: "small muted" }, "seen " + ago(u.last_seen_at)) : ""),
      h("td", {}, h("button", { class: "small-btn", onclick: () => editPerson(u) }, "Edit")))));
    if (!rows.length) tbody.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "muted" }, "Nobody here yet.")));
  };
  search.addEventListener("input", renderPeople);
  renderPeople();

  return h("div", {},
    h("h1", {}, "People & business units"),
    h("p", { class: "muted" }, "Business units own agents and their own variants of shared agents. People get roles per BU; members use the BU's agents in the chat app."),
    h("div", { class: "panel" }, h("div", { class: "row-head" }, h("h2", {}, "Business units"), bu.can_create ? h("button", { class: "primary", onclick: newBu }, "+ New business unit") : ""),
      h("div", { class: "bu-grid" }, bu.business_units.map(buCard))),
    people.users ? h("div", { class: "panel" },
      h("div", { class: "row-head" }, h("h2", {}, "People", onlyBu ? h("span", { class: "muted" }, ` in ${(busById[onlyBu] || { name: onlyBu }).name} `) : "",
        onlyBu ? h("a", { class: "small", href: "#/people" }, "show all") : ""),
        h("div", { class: "row-btns" }, search, manageable.length ? h("button", { class: "primary", onclick: () => editPerson(null) }, "+ Add person") : "")),
      h("table", { class: "people" }, h("thead", {}, h("tr", {}, ...["Person", "Business units & roles", "Default BU", "Status", ""].map((x) => h("th", {}, x)))), tbody)) :
      h("p", { class: "muted" }, "You can see the business units you belong to. BU admins manage their people here."),
    bu.can_create ? h("details", { class: "panel", ontoggle: async (e) => { if (e.target.open && !e.target.dataset.loaded) { e.target.dataset.loaded = 1; e.target.append(await viewRoles()); } } },
      h("summary", {}, "Raw role bindings (advanced)")) : "");
}

window.viewPeople = viewPeople;
