/* mcptab.js — the MCP Servers tab (singleton): Model Context Protocol
 * tool servers for chats. Servers are DEFINED in loom.yaml
 * (`mcp-servers:` — the wizard here writes entries non-destructively);
 * enable/disable starts/stops the process, and enabled servers autostart
 * when the library reopens. Each running server lists its tools with
 * their REAL function names (mcp_<server>_<tool>) — the name to use in
 * loom.yaml permission-modes overrides — and a per-tool DEFAULT
 * permission picked right here. */
"use strict";

function mcpState() {
  return st.mcpTab || (st.mcpTab = { servers: null });
}

function mountMcpTab(panel) {
  panel.classList.add("modstab");
  refreshMcpTab();
}

async function refreshMcpTab() {
  const ms = mcpState();
  const r = await Api.call("mcp_status");
  if (r.ok) ms.servers = r.data.servers;
  else if (ms.servers === null) { ms.servers = []; toast(r.error, "err"); }
  renderMcpTab();
}

const MCP_PERM_LEVELS = ["allow", "ask", "deny", "disabled"];

function renderMcpTab() {
  const panel = panelFor("mcpsrv");
  if (!panel) return;
  const ms = mcpState();
  const focus = captureFocus(panel);
  panel.replaceChildren();
  const wrap = el("div", { class: "mods-wrap" });

  wrap.append(el("div", { class: "srv-head" },
    el("h2", { text: "MCP Servers" }),
    el("div", { style: "display:flex;gap:6px" },
      el("button", { class: "btn btn-sm btn-acc", text: "New MCP server…",
        onclick: () => mcpWizard() }),
      el("button", { class: "btn btn-sm", text: "Edit loom.yaml",
        onclick: () => openLibraryFileAt(st.lib.configFile || "loom.yaml") }))));
  wrap.append(el("p", { class: "mods-hint",
    text: "Tool servers chats can call (Model Context Protocol, stdio). "
      + "Enabled servers autostart when this library reopens. Each tool's "
      + "default permission is set here; a permission mode in loom.yaml "
      + "can override per mode using the tool's full function name shown "
      + "below." }));

  if (ms.servers === null) {
    wrap.append(el("div", { class: "picker-empty", text: "Loading…" }));
  } else if (!ms.servers.length) {
    wrap.append(el("div", { class: "srv-empty" },
      el("p", { text: "No MCP servers in loom.yaml yet." }),
      el("div", { style: "display:flex;gap:8px;justify-content:center" },
        el("button", { class: "btn btn-acc", text: "New MCP server…",
          onclick: () => mcpWizard() }))));
  }
  for (const s of ms.servers || []) wrap.append(mcpServerCard(s));

  if (ms.servers?.length) {
    const example =
      "permission-modes:\n"
      + "  always-ask:\n"
      + "    tools:\n"
      + "      mcp_<server>_<tool>: deny   # allow | ask | deny | disabled";
    const copyBtn = el("button", { class: "btn btn-sm",
      html: icon("copy", 12) + " Copy", title: "Copy the example" });
    copyBtn.addEventListener("click", async () => {
      await copyText(example);
      copyBtn.textContent = "✓ copied";
      setTimeout(() => { copyBtn.innerHTML = icon("copy", 12) + " Copy"; }, 1500);
    });
    wrap.append(el("div", { class: "mods-list" },
      el("div", { class: "mods-dir", style: "display:flex;align-items:center;gap:8px" },
        el("span", { text: "per-mode overrides in loom.yaml" }),
        el("span", { style: "flex:1" }), copyBtn),
      el("div", { class: "api-usage" },
        el("pre", { text: example }),
        el("p", { class: "mods-hint",
          text: "An entry like this wins over the defaults above for that "
            + "one mode. The exact function names are listed on each "
            + "tool row — click one to copy it." }))));
  }

  panel.append(wrap);
  restoreFocus(panel, focus);
}

function mcpServerCard(s) {
  const card = el("div", { class: "srv-card" });
  const row = el("div", { class: "srv-row" },
    el("span", { class: "dot " + (s.running ? "running" : "stopped") }),
    el("span", { class: "srv-name", text: s.name }),
    el("span", { class: "srv-state" },
      el("span", { text: s.running ? "running · " + s.tools.length + " tools"
        : "stopped" })));
  const actions = el("div", { class: "srv-actions" });
  if (s.running) {
    actions.append(
      el("button", { class: "btn btn-sm", text: "Refresh tools",
        title: "Re-query the server's tool list",
        onclick: async () => {
          const r = await Api.call("mcp_refresh", s.name);
          if (!r.ok) { toast(r.error, "err"); return; }
          mcpState().servers = r.data.servers;
          renderMcpTab();
        } }),
      el("button", { class: "btn btn-sm btn-danger", text: "Disable",
        onclick: () => mcpToggle(s.name, false) }));
  } else {
    actions.append(el("button", { class: "btn btn-sm btn-acc", text: "Enable",
      onclick: () => mcpToggle(s.name, true) }));
  }
  row.append(actions);
  card.append(row);
  card.append(el("div", { class: "srv-meta", title: s.command, text: s.command }));
  if (s.error && s.error !== "stopped") {
    card.append(el("div", { class: "srv-detail error", text: s.error }));
  }

  for (const t of s.tools || []) {
    const fn = el("code", { class: "mcp-fn", text: t.fullName,
      title: "The real function name — use it in permission-modes. Click "
        + "to copy." });
    fn.addEventListener("click", async () => {
      await copyText(t.fullName);
      toast("Copied " + t.fullName, "ok", 1200);
    });
    const sel = el("select", { class: "term-sel mcp-perm-sel",
      title: "Default permission for this tool (any chat mode without an "
        + "explicit loom.yaml override)" });
    for (const lv of MCP_PERM_LEVELS) sel.append(el("option", { value: lv, text: lv }));
    sel.value = t.perm;
    sel.addEventListener("change", async () => {
      const r = await Api.call("mcp_tool_perm_set", t.fullName, sel.value);
      if (!r.ok) { toast(r.error, "err"); return; }
      mcpState().servers = r.data.servers;
    });
    card.append(el("div", { class: "mcp-tool" },
      el("span", { class: "mcp-tool-col" },
        el("span", { class: "mcp-tool-name", text: t.name }),
        fn,
        t.description ? el("span", { class: "mcp-tool-desc",
          text: t.description }) : null),
      sel));
  }
  return card;
}

async function mcpToggle(name, on) {
  const r = await Api.call("mcp_toggle", name, on);
  if (!r.ok) { toast(r.error, "err"); refreshMcpTab(); return; }
  mcpState().servers = r.data.servers;
  renderMcpTab();
  toast((on ? "Enabled " : "Disabled ") + name, "ok", 1500);
}

/* ---- the setup wizard: name + command + env → loom.yaml entry ---- */
function mcpWizard() {
  const body = el("div", { class: "wiz-body" });
  modal("New MCP server", [body], [{ label: "Cancel" }], { id: "mcp-wizard" });
  const nameIn = el("input", { type: "text",
    placeholder: "name — letters/digits/-/_ (part of tool function names)" });
  const cmdIn = el("input", { type: "text",
    placeholder: "command, e.g. npx -y @modelcontextprotocol/server-filesystem /tmp" });
  const envIn = el("textarea", { class: "mcp-env",
    placeholder: "environment (optional) — one KEY=value per line", rows: "3" });
  const add = el("button", { class: "btn btn-acc", text: "Add to loom.yaml",
    onclick: async () => {
      const env = {};
      for (const ln of envIn.value.split("\n")) {
        const s2 = ln.trim();
        if (!s2) continue;
        const i = s2.indexOf("=");
        if (i < 1) { toast("env lines are KEY=value: " + s2, "err"); return; }
        env[s2.slice(0, i).trim()] = s2.slice(i + 1).trim();
      }
      const r = await Api.call("mcp_add", nameIn.value.trim(),
        cmdIn.value.trim(), env);
      if (!r.ok) { toast(r.error, "err"); return; }
      mcpState().servers = r.data.servers;
      closeTopModal();
      renderMcpTab();
      libReloadIfOpen(st.lib.configFile || "loom.yaml");
      toast("Added — enable it to connect and see its tools.", "ok");
    } });
  body.append(
    el("p", { class: "wiz-hint",
      text: "A stdio MCP server: Loom runs the command and speaks MCP on "
        + "its stdin/stdout. The entry lands in loom.yaml's mcp-servers: "
        + "block." }),
    el("div", { class: "ts-row" }, el("label", { text: "Name" }), nameIn),
    el("div", { class: "ts-row" }, el("label", { text: "Command" }), cmdIn),
    el("div", { class: "ts-row" }, el("label", { text: "Env" }), envIn),
    el("div", { class: "wiz-nav" }, el("span", { class: "spacer" }), add));
  setTimeout(() => nameIn.focus(), 0);
}

/* bus events: server started/stopped/died elsewhere (autostart, errors) */
function onMcpEvent(ev) {
  if (ev.kind === "error") {
    toast("MCP " + (ev.name || "") + ": " + (ev.msg || "failed"), "err");
  }
  if (st.activeTab === "mcpsrv") refreshMcpTab();
}
