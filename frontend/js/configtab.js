/* configtab.js - the Configuration tab (singleton): the WHOLE loom.yaml,
 * editable two ways. The raw editor saves through config_write, which
 * validates the complete candidate before a byte lands on disk and then
 * converges the runtime (provider re-probe, API server rebind, dropped
 * MCP servers stopped). The section dialogs (chat defaults, permission
 * modes, containers) write minimal, comment-preserving blocks through
 * their own endpoints - hand-written comments elsewhere survive. */
"use strict";

/* builtin permission modes + core tool names: product constants, mirrored
 * from loom/libconfig.py (BUILTIN_MODES, READ_TOOLS/EDIT_TOOLS) */
const CFG_BUILTIN_MODES = ["always-ask", "allow-edits", "always-allow"];
const CFG_CORE_TOOLS = ["knowledge_search", "read_file", "list_dir", "grep",
  "find_files", "write_file", "edit_file", "shell"];
const CFG_PERM_LEVELS = ["allow", "ask", "deny", "disabled"];

function cfgTabState() {
  return st.cfgTab || (st.cfgTab = {
    editor: null, dirty: false, mtime: null,
    file: "loom.yaml", scrollPos: 0,
  });
}

function mountConfigTab(panel) {
  panel.classList.add("cfgtab");
  panel.append(
    el("div", { class: "srv-head cfg-head" },
      el("h2", { text: "Configuration" }),
      el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" },
        el("button", { class: "btn btn-sm", text: "Chat defaults…",
          title: "Default provider, model, permission mode, prompts and "
            + "compaction for new chats",
          onclick: () => chatDefaultsDialog() }),
        el("button", { class: "btn btn-sm", text: "Permission modes…",
          title: "What each mode lets tools do - allow / ask / deny / "
            + "disabled per tool",
          onclick: () => permissionModesDialog() }),
        el("button", { class: "btn btn-sm", text: "Containers…",
          title: "Container engine and sandbox definitions for chats "
            + "and terminals",
          onclick: () => containersDialog() }),
        el("button", { class: "btn btn-sm",
          html: icon("servers", 13) + " Providers",
          onclick: () => openTab("servers") }),
        el("button", { class: "btn btn-sm", html: icon("mcp", 13) + " MCP",
          onclick: () => openTab("mcpsrv") }),
        el("button", { class: "btn btn-sm", html: icon("globe", 13) + " API",
          onclick: () => openTab("apisrv") }))),
    el("p", { class: "mods-hint cfg-hint",
      text: "The raw file, validated on save: a candidate that would not "
        + "parse is rejected with the exact error and nothing is written. "
        + "A successful save applies live - providers re-probe, a running "
        + "API server rebinds if its address changed. Comments are yours; "
        + "Loom's own edits never touch them." }),
    el("div", { class: "cfg-filebar", "data-role": "cfg-filebar" }),
    el("div", { class: "cfg-editor", "data-role": "cfg-editor" }));
  refreshConfigTab();
}

/* (re)load the buffer from disk. Never clobbers unsaved edits unless
 * `force` (the Discard button) says so. */
async function refreshConfigTab(force) {
  const panel = panelFor("config");
  if (!panel) return;
  const cs = cfgTabState();
  if (cs.editor && cs.dirty && !force) { renderConfigFilebar(); return; }
  const r = await Api.call("config_read");
  if (!r.ok) { toast(r.error, "err"); return; }
  cs.file = r.data.rel || "loom.yaml";
  cs.mtime = r.data.mtime;
  if (!cs.editor) {
    const host = panel.querySelector('[data-role="cfg-editor"]');
    host.replaceChildren();
    cs.editor = new LoomEditor(host, {
      text: r.data.text, mode: "code", lang: "yaml",
      onDirty: (d) => { cs.dirty = d; renderConfigFilebar(); renderTabs(); },
      onSave: () => saveConfigTab(),
    });
    cs.editor.scroller.addEventListener("scroll", () => {
      if (cs.editor.scroller.clientHeight) {
        cs.scrollPos = cs.editor.scroller.scrollTop;
      }
    }, { passive: true });
  } else if (cs.editor.getValue() !== r.data.text) {
    const sc = cs.editor.scroller;
    const pos = sc.scrollTop;
    cs.editor.setText(r.data.text);
    requestAnimationFrame(() => { sc.scrollTop = pos; });
  } else {
    cs.editor.markClean();
  }
  cs.dirty = false;
  renderConfigFilebar();
  renderTabs();
}

async function saveConfigTab() {
  const cs = cfgTabState();
  if (!cs.editor) return false;
  const r = await Api.call("config_write", cs.editor.getValue(), cs.mtime);
  if (!r.ok) { toast(r.error, "err"); return false; }
  cs.mtime = r.data.mtime;
  cs.dirty = false;
  cs.editor.markClean();
  renderConfigFilebar();
  renderTabs();
  toast("Saved " + cs.file + " - config applied", "ok", 2200);
  libReloadIfOpen(st.lib.configFile || cs.file);
  return true;
}

function renderConfigFilebar() {
  const bar = panelFor("config")?.querySelector('[data-role="cfg-filebar"]');
  if (!bar) return;
  const cs = cfgTabState();
  bar.replaceChildren(
    el("span", { html: icon("yaml", 13) }),
    el("span", { class: "lib-filename" + (cs.dirty ? " dirty" : ""),
      text: cs.file }),
    el("span", { class: "cfg-note",
      text: cs.dirty ? "unsaved changes" : "" }),
    el("span", { class: "spacer" }));
  if (cs.dirty) {
    bar.append(
      el("button", { class: "btn btn-sm", text: "Discard",
        onclick: () => confirmModal("Discard changes",
          "Throw away the unsaved changes to " + cs.file + "?",
          "Discard", () => refreshConfigTab(true), true) }),
      el("button", { class: "btn btn-sm btn-acc", text: "Save  (Ctrl+S)",
        onclick: saveConfigTab }));
  } else {
    bar.append(el("button", { class: "btn btn-sm",
      html: icon("refresh", 12) + " Reload",
      title: "Re-read " + cs.file + " from disk",
      onclick: () => refreshConfigTab(true) }));
  }
}

/* another writer touched loom.yaml (a dialog, the Providers tab, the
 * Library tab): reload the raw buffer so it never shows a stale file.
 * Unsaved edits are kept - saving them will hit the mtime gate. */
function configReloadIfOpen() {
  const cs = st.cfgTab;
  if (!cs || !cs.editor) return;
  if (cs.dirty) {
    if (st.activeTab === "config") {
      toast(cs.file + " changed on disk. Your unsaved edits are kept - "
        + "Discard to load the new version.", "warn", 6000);
    }
    return;
  }
  refreshConfigTab();
}

/* ---------- shared dialog bits ---------- */

function cfgNeedsValid() {
  if (st.config?.error) {
    toast("Fix loom.yaml first - " + st.config.error, "err");
    return null;
  }
  return st.config || {};
}

function cfgRow(label, field) {
  return el("div", { class: "ts-row" }, el("label", { text: label }), field);
}

/* ---------- Chat defaults ---------- */

function chatDefaultsDialog() {
  const cfg = cfgNeedsValid();
  if (!cfg) return;
  const c = cfg.chat || {};

  const provSel = el("select", { class: "term-sel" },
    el("option", { value: "", text: "(first provider)" }));
  for (const p of cfg.providers || []) {
    provSel.append(el("option", { value: p.name, text: p.name }));
  }
  provSel.value = c.provider || "";

  const modelIn = el("input", { type: "text", list: "cfg-model-list",
    value: c.model || "", placeholder: "model id, as the provider lists it" });
  const dl = el("datalist", { id: "cfg-model-list" });
  const seen = new Set();
  for (const [pname, live] of Object.entries(st.providers || {})) {
    for (const m of live.models || []) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      dl.append(el("option", { value: m.id, label: pname }));
    }
  }

  const modeSel = el("select", { class: "term-sel" });
  for (const name of Object.keys(cfg.permissionModes || {})) {
    modeSel.append(el("option", { value: name, text: name }));
  }
  modeSel.value = c.permission_mode || "always-ask";

  const sysIn = el("input", { type: "text", value: c.system_prompt || "",
    placeholder: "prompts/system.md" });
  const compIn = el("input", { type: "text", value: c.compaction_prompt || "",
    placeholder: "prompts/compaction.md" });
  const titleIn = el("input", { type: "text", value: c.title_prompt || "",
    placeholder: "prompts/title.md" });

  const truncCk = el("input", { type: "checkbox" });
  truncCk.checked = c.thought_truncation !== false;
  const sigCk = el("input", { type: "checkbox" });
  sigCk.checked = c.assistant_signals !== false;
  const nameIn = el("input", { type: "text", placeholder: "loom",
    value: c.assistant_name && c.assistant_name !== "loom"
      ? c.assistant_name : "" });
  const autoCk = el("input", { type: "checkbox" });
  autoCk.checked = (c.compaction?.auto) !== false;
  const thrIn = el("input", { type: "number", class: "cfg-num",
    min: "0.2", max: "0.95", step: "0.05",
    value: String(c.compaction?.threshold ?? 0.8) });

  modal("Chat defaults",
    [el("p", { class: "wiz-hint",
       text: "Defaults for NEW chats (existing chats keep their own "
         + "settings). Values matching Loom's defaults are left out of "
         + "loom.yaml, so the file stays minimal." }),
     cfgRow("Provider", provSel),
     cfgRow("Model", modelIn), dl,
     cfgRow("Permissions", modeSel),
     cfgRow("Agent name", nameIn),
     cfgRow("System", sysIn),
     cfgRow("Compaction", compIn),
     cfgRow("Title", titleIn),
     el("label", { class: "chk" }, truncCk,
       "Drop older thinking from the wire (the last turn's is kept) - "
       + "the default for NEW chats; each chat's thoughts chip overrides"),
     el("label", { class: "chk" }, sigCk,
       "Assistant time signals - replies carry their generation time"),
     el("label", { class: "chk" }, autoCk,
       "Compact automatically near the context limit"),
     cfgRow("Threshold", thrIn)],
    [
      { label: "Cancel" },
      {
        label: "Save to loom.yaml", cls: "btn-acc",
        fn: () => {
          const thr = Number(thrIn.value);
          if (!Number.isFinite(thr) || thr < 0.2 || thr > 0.95) {
            toast("The compaction threshold must be 0.2-0.95.", "warn");
            return false;
          }
          Api.call("chat_config_set", {
            provider: provSel.value,
            model: modelIn.value.trim(),
            permission_mode: modeSel.value,
            system_prompt: sysIn.value.trim(),
            compaction_prompt: compIn.value.trim(),
            title_prompt: titleIn.value.trim(),
            thought_truncation: truncCk.checked,
            assistant_signals: sigCk.checked,
            assistant_name: nameIn.value.trim() || "loom",
            compaction: { auto: autoCk.checked, threshold: thr },
          }).then((r) => {
            if (!r.ok) { toast(r.error, "err"); return; }
            toast("Chat defaults saved to loom.yaml", "ok");
            libReloadIfOpen(st.lib.configFile || "loom.yaml");
          });
        },
      },
    ], { id: "cfg-chat" });
}

/* ---------- Permission modes ---------- */

function permissionModesDialog() {
  const cfg = cfgNeedsValid();
  if (!cfg) return;
  // work on a deep copy of the EFFECTIVE maps; the backend writes only
  // the differences from the built-ins back to loom.yaml
  const modes = {};
  for (const [name, tools] of Object.entries(cfg.permissionModes || {})) {
    modes[name] = { ...tools };
  }
  for (const name of CFG_BUILTIN_MODES) modes[name] = modes[name] || {};

  const body = el("div", { class: "permx-body" });

  const toolRows = () => {
    const extra = new Set();
    for (const tools of Object.values(modes)) {
      for (const t of Object.keys(tools)) {
        if (!CFG_CORE_TOOLS.includes(t)) extra.add(t);
      }
    }
    return CFG_CORE_TOOLS.concat([...extra].sort());
  };

  const render = () => {
    body.replaceChildren();
    const names = Object.keys(modes);
    const table = el("table", { class: "permx" });
    const head = el("tr", {}, el("th", { text: "tool" }));
    for (const name of names) {
      const th = el("th", {}, el("span", { text: name }));
      if (!CFG_BUILTIN_MODES.includes(name)) {
        th.append(el("button", { class: "permx-del", text: "×",
          title: "Remove this mode from loom.yaml",
          onclick: () => { delete modes[name]; render(); } }));
      }
      head.append(th);
    }
    table.append(head);
    for (const tool of toolRows()) {
      const tr = el("tr", {}, el("td", { class: "permx-tool", text: tool }));
      for (const name of names) {
        const sel = el("select", { class: "term-sel permx-sel" });
        for (const lv of CFG_PERM_LEVELS) {
          sel.append(el("option", { value: lv, text: lv }));
        }
        sel.value = modes[name][tool] || "ask";
        sel.addEventListener("change", () => {
          modes[name][tool] = sel.value;
        });
        tr.append(el("td", {}, sel));
      }
      table.append(tr);
    }
    body.append(el("div", { class: "permx-scroll" }, table));

    const modeIn = el("input", { type: "text", class: "permx-add",
      placeholder: "new mode name" });
    const addMode = el("button", { class: "btn btn-sm", text: "Add mode",
      onclick: () => {
        const n = modeIn.value.trim();
        if (!n) return;
        if (modes[n]) { toast("There already is a mode called " + n, "warn"); return; }
        modes[n] = { ...(modes["always-ask"] || {}) };
        render();
      } });
    const toolIn = el("input", { type: "text", class: "permx-add",
      placeholder: "tool name, e.g. mcp_files_read_file" });
    const addTool = el("button", { class: "btn btn-sm", text: "Add tool row",
      onclick: () => {
        const t = toolIn.value.trim();
        if (!t) return;
        for (const name of Object.keys(modes)) {
          if (!(t in modes[name])) modes[name][t] = "ask";
        }
        render();
      } });
    body.append(el("div", { class: "permx-foot" },
      modeIn, addMode, el("span", { class: "spacer" }), toolIn, addTool));
  };
  render();

  const m = modal("Permission modes",
    [el("p", { class: "wiz-hint",
       text: "A chat runs under exactly one mode. disabled = the tool is "
         + "not even offered to the model. Only differences from the "
         + "built-in modes are written to loom.yaml; a built-in column "
         + "put back to its defaults disappears from the file." }),
     body],
    [
      { label: "Cancel" },
      {
        label: "Save to loom.yaml", cls: "btn-acc",
        fn: () => {
          Api.call("permission_modes_set", modes).then((r) => {
            if (!r.ok) { toast(r.error, "err"); return; }
            toast("Permission modes saved to loom.yaml", "ok");
            libReloadIfOpen(st.lib.configFile || "loom.yaml");
          });
        },
      },
    ], { id: "cfg-perms" });
  m.classList.add("modal-wide");
}

/* ---------- Containers ---------- */

function containersDialog() {
  const cfg = cfgNeedsValid();
  if (!cfg) return;
  const c = cfg.containers || {};
  const defs = (c.definitions || []).map((d) => ({ ...d }));

  const engineSel = el("select", { class: "term-sel" },
    el("option", { value: "auto", text: "auto (podman, then docker)" }),
    el("option", { value: "podman", text: "podman" }),
    el("option", { value: "docker", text: "docker" }));
  engineSel.value = c.engine || "auto";
  const defaultIn = el("input", { type: "text", value: c.default || "",
    placeholder: "sandbox" });

  const defsBox = el("div", { class: "cfg-defs" });
  const render = () => {
    defsBox.replaceChildren();
    defs.forEach((d, i) => {
      const nameIn = el("input", { type: "text", value: d.name || "",
        placeholder: "name" });
      nameIn.addEventListener("input", () => { d.name = nameIn.value; });
      const fileIn = el("input", { type: "text", value: d.file || "",
        placeholder: "Containerfile path (optional)" });
      fileIn.addEventListener("input", () => { d.file = fileIn.value; });
      defsBox.append(el("div", { class: "cfg-def-row" },
        nameIn, fileIn,
        el("button", { class: "btn btn-sm", text: "×",
          title: "Remove this definition",
          onclick: () => { defs.splice(i, 1); render(); } })));
    });
    defsBox.append(el("button", { class: "btn btn-sm",
      text: "Add definition",
      onclick: () => { defs.push({ name: "", file: "" }); render(); } }));
  };
  render();

  modal("Containers",
    [el("p", { class: "wiz-hint",
       text: "The engine that runs chat/terminal sandboxes, the default "
         + "container for new ones, and named definitions built from "
         + "Containerfiles in the library." }),
     cfgRow("Engine", engineSel),
     cfgRow("Default", defaultIn),
     el("div", { class: "ts-row" }, el("label", { text: "Definitions" }), defsBox)],
    [
      { label: "Cancel" },
      {
        label: "Save to loom.yaml", cls: "btn-acc",
        fn: () => {
          const rows = defs.filter((d) => (d.name || "").trim());
          if (rows.length !== defs.length) {
            toast("Every definition needs a name (or remove the row).", "warn");
            return false;
          }
          Api.call("containers_config_set", {
            engine: engineSel.value,
            default: defaultIn.value.trim(),
            definitions: rows.map((d) => ({ name: d.name.trim(),
                                            file: (d.file || "").trim() })),
          }).then((r) => {
            if (!r.ok) { toast(r.error, "err"); return; }
            toast("Container settings saved to loom.yaml", "ok");
            libReloadIfOpen(st.lib.configFile || "loom.yaml");
          });
        },
      },
    ], { id: "cfg-containers" });
}
