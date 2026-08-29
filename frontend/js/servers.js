/* servers.js — the Servers tab (singleton): every model from loom.yaml as
 * a card with live state, start/stop/restart, a log viewer, and a button
 * that jumps to loom.yaml in the Library tab. */
"use strict";

function mountServersTab(panel) {
  panel.classList.add("srvtab");
  panel.append(el("div", { class: "srv-cards", "data-role": "cards" }));
  refreshServersTab();
}

async function refreshServersTab() {
  const panel = panelFor("servers");
  if (!panel) return;
  const res = await Api.call("servers_get");
  if (!res.ok) { toast(res.error, "err"); return; }
  st.serverModels = res.data.models;
  st.config = res.data.config;
  renderServersTab();
}

function renderServersTab() {
  const panel = panelFor("servers");
  if (!panel) return;
  // never rebuild the panel out from under a live text selection — that is
  // what made copying the log impossible; retry once the selection is gone
  const s = getSelection();
  if (s && s.rangeCount && !s.isCollapsed && panel.contains(s.anchorNode)) {
    clearTimeout(renderServersTab._retry);
    renderServersTab._retry = setTimeout(renderServersTab, 1200);
    return;
  }
  panel.replaceChildren();

  panel.append(el("div", { class: "srv-head" },
    el("h2", { text: "Servers" }),
    el("div", { style: "display:flex;gap:6px" },
      el("button", {
        class: "btn btn-sm btn-acc", text: "New model…",
        onclick: () => modelWizard(),
      }),
      el("button", {
        class: "btn btn-sm", text: "Edit loom.yaml",
        onclick: () => openLibraryFileAt(st.lib.configFile || "loom.yaml"),
      }))));

  if (st.config?.error) {
    panel.append(el("div", { class: "srv-cfgerr", text: "loom.yaml: " + st.config.error }));
    return;
  }
  // pinned models first (stable sort keeps loom.yaml order within groups)
  const pinnedSet = new Set(st.pins || []);
  const models = [...(st.serverModels || [])].sort((a, b) =>
    (pinnedSet.has(b.id) ? 1 : 0) - (pinnedSet.has(a.id) ? 1 : 0));
  if (!models.length) {
    panel.append(el("div", { class: "srv-empty" },
      el("p", { text: "No models defined yet." }),
      el("p", { text: "The wizard finds a GGUF, pairs its vision projector, sizes the context, and writes the loom.yaml entry for you." }),
      el("div", { style: "display:flex;gap:8px;justify-content:center" },
        el("button", { class: "btn btn-acc", text: "New model…", onclick: () => modelWizard() }),
        el("button", {
          class: "btn", text: "Open loom.yaml",
          onclick: () => openLibraryFileAt(st.lib.configFile || "loom.yaml"),
        }))));
    return;
  }
  const cards = el("div", { class: "srv-cards" });
  for (const m of models) cards.append(serverCard(m));
  panel.append(cards);
}

function serverCard(m) {
  const live = st.servers[m.id] || {};
  const state = live.state || m.state || "stopped";
  const detail = live.detail ?? m.detail ?? "";
  const busy = ["starting", "loading", "stopping"].includes(state);
  const up = ["running", "starting", "loading"].includes(state);

  const card = el("div", { class: "srv-card" });
  const row = el("div", { class: "srv-row" },
    el("span", { class: "dot " + state }),
    el("span", { class: "srv-name", text: m.name }),
    el("span", { class: "srv-host", text: m.host ? "ssh: " + m.host : "local" }),
    el("span", { class: "srv-state" },
      el("span", { text: state + (live.nCtx ? ` · ${Math.round(live.nCtx / 1000)}k ctx` : "") })));
  const actions = el("div", { class: "srv-actions" });
  if (up) {
    actions.append(
      el("button", { class: "btn btn-sm", text: "Restart", disabled: busy ? "" : null, onclick: () => Api.call("server_restart", m.id) }),
      el("button", { class: "btn btn-sm btn-danger", text: "Stop", onclick: () => Api.call("server_stop", m.id) }));
  } else {
    actions.append(el("button", {
      class: "btn btn-sm btn-acc", text: "Start", disabled: busy ? "" : null,
      onclick: () => Api.call("server_start", m.id),
    }));
  }
  actions.append(el("button", {
    class: "btn btn-sm", text: st.serverLogOpen[m.id] ? "Hide log" : "Log",
    onclick: () => toggleServerLog(m.id),
  }));
  row.append(actions);
  card.append(row);

  card.append(el("div", { class: "srv-meta", title: m.command || m.model, text: m.model + (m.ctx ? `  ·  -c ${m.ctx}` : "") }));
  if (m.configError) {
    card.append(el("div", { class: "srv-detail error", text: "flags problem: " + m.configError }));
  }
  const note = st.notes[m.id];
  if (note) card.append(el("div", { class: "srv-note", text: note }));
  else if (detail) {
    card.append(el("div", {
      class: "srv-detail" + (state === "error" ? " error" : ""),
      text: detail,
    }));
  }
  if (st.serverLogOpen[m.id]) {
    const copyBtn = el("button", { class: "btn btn-sm", html: icon("copy", 12) + " Copy", title: "Copy the whole log" });
    copyBtn.addEventListener("click", async () => {
      await copyText(st.serverLogs[m.id] || "");
      copyBtn.textContent = "✓ copied";
      setTimeout(() => { copyBtn.innerHTML = icon("copy", 12) + " Copy"; }, 1500);
    });
    card.append(el("div", { class: "srv-logbar" },
      el("span", { class: "lbl", text: "server log (live)" }),
      copyBtn,
      el("button", {
        class: "btn btn-sm", text: "Clear",
        title: "Truncate the log on its host — the server keeps appending",
        onclick: async () => {
          const r = await Api.call("server_log_clear", m.id);
          if (!r.ok) { toast(r.error, "err"); return; }
          st.serverLogs[m.id] = "";
          patchServerLog(m.id);
        },
      })));
    const log = el("div", {
      class: "srv-log", "data-log-for": m.id,
      text: st.serverLogs[m.id] ?? "loading log…",
    });
    card.append(log);
    requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }
  return card;
}

/* ---------- live log following ----------
 * Open logs STREAM: a light poll patches the log element in place —
 * never a full panel re-render — sticks to the bottom like a terminal
 * unless the user scrolled up, and never yanks a text selection. */
const SRV_LOG_POLL_MS = 2000;
let _srvLogTimer = null;

function ensureSrvLogPoll() {
  if (_srvLogTimer) return;
  _srvLogTimer = setInterval(async () => {
    if (st.activeTab !== "servers") return;
    for (const mid of Object.keys(st.serverLogOpen)) {
      if (!st.serverLogOpen[mid]) continue;
      const res = await Api.call("server_log", mid, 64000);
      if (!res.ok) continue;
      const text = res.data.log || "(empty)";
      if (text === st.serverLogs[mid]) continue;
      st.serverLogs[mid] = text;
      patchServerLog(mid);
    }
  }, SRV_LOG_POLL_MS);
}

function patchServerLog(mid) {
  const node = panelFor("servers")
    ?.querySelector(`[data-log-for="${CSS.escape(mid)}"]`);
  if (!node) return;
  const s = getSelection();
  if (s && s.rangeCount && !s.isCollapsed && node.contains(s.anchorNode)) {
    return;   // the user is copying — the next tick catches up
  }
  const stick = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
  node.textContent = st.serverLogs[mid] || "(empty)";
  if (stick) node.scrollTop = node.scrollHeight;
}

async function fetchServerLog(mid) {
  const res = await Api.call("server_log", mid, 64000);
  st.serverLogs[mid] = res.ok ? (res.data.log || "(empty)") : "error: " + res.error;
  renderServersTab();
}

function toggleServerLog(mid) {
  st.serverLogOpen[mid] = !st.serverLogOpen[mid];
  renderServersTab();
  if (st.serverLogOpen[mid]) {
    fetchServerLog(mid);
    ensureSrvLogPoll();
  }
}

/* ==================== the new-model wizard ====================
 * Three lean steps, minimal chrome:
 *   1  pick    — host chips + filter + GGUF list; CHOOSING A FILE is the
 *                next button (click, or arrows + Enter from the filter)
 *   2  shape   — name / context / backend / binary / vision projector
 *   3  review  — the exact yaml, editable, one Add button
 * Non-destructive: the snippet lands at the end of the models: block. */

const WIZ_BACKENDS = ["vulkan", "cuda", "hip", "sycl", "blas", "openvino", "cpu"];

function wizDisplayName(p) {
  let b = baseName(p).replace(/\.gguf$/i, "");
  b = b.replace(/[-_.](Q\d[\w.]*|IQ\d[\w.]*|BF16|F16|F32)$/i, "");
  return b.replace(/[-_]+/g, " ").trim() || baseName(p);
}

/* prefill {host, path}: launched from a Models-tab row the wizard scans
 * that host and, when the file is still there, jumps straight to step 2 */
function modelWizard(prefill) {
  const W = { step: 1, host: prefill?.host || "", hosts: [], entries: null,
    scanning: false, autopick: prefill?.path || null,
    filter: "", sel: null, mmproj: null, useMm: true,
    name: "", ctx: 131072, ctxMax: 131072, metaKnown: false,
    backend: "vulkan", binary: "llama-server", snippet: "" };
  Api.call("models_hosts").then((r) => {
    if (r.ok) { W.hosts = r.data.hosts; render(); }
  });

  const body = el("div", { class: "wiz-body" });
  modal("New model", [body], [{ label: "Cancel" }], { id: "model-wizard" });

  const flagsFor = (be) => {
    const gpu = !["cpu", "blas"].includes(be);
    if (!gpu) return ["# backend: " + be, "-ngl 0", "-np 1"];
    // safe everyday set. Speculative decoding ships COMMENTED OUT: only
    // models with MTP/draft layers support it — on any other model the
    // server FAILS TO START ("failed to create MTP context")
    return ["# backend: " + be,
            "-ngl 99",
            "-fa on",
            "-ctk q4_0 -ctv q4_0",
            "# speculative decoding — uncomment ONLY if this model ships",
            "# MTP layers (otherwise llama-server exits at load):",
            "# --spec-type draft-mtp",
            "# --spec-draft-n-max 2",
            "# --spec-draft-n-min 0",
            "# --spec-draft-p-min 0.75",
            "-np 1"];
  };
  const entry = () => ({
    name: W.name.trim(), host: W.host, context: W.ctx, path: W.sel,
    mmproj: (W.useMm && W.mmproj) ? W.mmproj : "",
    binary: W.binary, flags: flagsFor(W.backend),
  });

  async function scan() {
    W.scanning = true;
    render();
    const r = await Api.call("models_scan", W.host);
    W.scanning = false;
    W.entries = r.ok ? r.data.entries : [];
    if (!r.ok) toast(r.error, "err");
    const pick = W.autopick;
    W.autopick = null;
    if (pick && W.entries.some((e2) => e2.path === pick)) {
      choose(pick);      // the prefilled file → straight to the shape step
      return;
    }
    render();
  }

  /* host chips rescan immediately — no confirm step */
  function setHost(h) {
    if (W.host === h && W.entries !== null) return;
    W.host = h;
    W.entries = null;
    W.sel = null;
    render();
    scan();
  }

  function mmMap() {
    const out = {};
    for (const e2 of W.entries || []) {
      if (e2.mmproj && e2.pairedWith) out[e2.pairedWith] = e2.path;
    }
    return out;
  }

  const filteredRows = () => {
    const terms = W.filter.toLowerCase().split(/\s+/).filter(Boolean);
    return (W.entries || []).filter((e2) => !e2.mmproj
      && terms.every((t) => e2.path.toLowerCase().includes(t)));
  };

  /* picking a model IS the next button */
  async function choose(path) {
    W.sel = path;
    W.mmproj = mmMap()[path] || null;
    W.useMm = !!W.mmproj;
    if (!W.name) W.name = wizDisplayName(path);
    W.step = 2;
    render();
    const r = await Api.call("models_meta", W.sel, W.host);
    const ctx = r.ok ? Number(r.data.meta?.ctx) || 0 : 0;
    W.metaKnown = !!ctx;
    W.ctxMax = ctx || 131072;
    W.ctx = W.ctxMax;   // the slider starts at the model's max
    render();
  }

  function render() {
    body.replaceChildren();

    if (W.step === 1) {
      const chips = el("div", { class: "mods-hosts" },
        el("button", { class: "btn btn-sm" + (!W.host ? " btn-acc" : ""),
          text: "localhost", onclick: () => setHost("") }),
        ...W.hosts.map((h) => el("button", {
          class: "btn btn-sm" + (W.host === h ? " btn-acc" : ""), text: h,
          onclick: () => setHost(h) })),
        el("button", { class: "btn btn-sm", text: "+ ssh host…",
          onclick: () => promptModal("Add a model host",
            "ssh destination (user@host or a ~/.ssh/config alias):", "",
            async (v) => {
              const dest = v.trim();
              if (!dest) return;
              const r = await Api.call("models_host_add", dest);
              if (!r.ok) { toast(r.error, "err"); return; }
              W.hosts = r.data.hosts;
              setHost(dest);
            }, "Add") }));
      const filterIn = el("input", { type: "text", value: W.filter,
        placeholder: "filter — ↑/↓ highlight · Enter or click picks" });
      const list = el("div", { class: "wiz-list" });
      const renderList = () => {
        list.replaceChildren();
        const rows = filteredRows();
        if (!rows.length) {
          list.append(el("div", { class: "picker-empty",
            text: W.scanning ? "Scanning…" : "No .gguf files found." }));
        }
        for (const e2 of rows) {
          list.append(el("div", {
            class: "wiz-row" + (W.sel === e2.path ? " sel" : ""),
            text: e2.path, title: e2.path,
            onclick: () => choose(e2.path),
          }));
        }
        list.querySelector(".sel")?.scrollIntoView({ block: "nearest" });
      };
      filterIn.addEventListener("input", () => { W.filter = filterIn.value; renderList(); });
      filterIn.addEventListener("keydown", (e) => {
        const rows = filteredRows();
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          if (!rows.length) return;
          const i = rows.findIndex((r) => r.path === W.sel);
          const j = i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1,
            i + (e.key === "ArrowDown" ? 1 : -1)));
          W.sel = rows[j].path;
          renderList();
        } else if (e.key === "Enter" && W.sel
                   && rows.some((r) => r.path === W.sel)) {
          e.preventDefault();
          choose(W.sel);
        }
      });
      const rescan = el("button", { class: "iconbtn",
        title: "Rescan this host", html: icon("refresh", 14),
        disabled: W.scanning ? "" : null, onclick: scan });
      body.append(
        el("p", { class: "wiz-hint",
          text: "Where does llama-server run — and which GGUF? Picking one moves on." }),
        chips,
        el("div", { class: "wiz-filterrow" }, filterIn, rescan),
        list);
      renderList();
      if (W.entries === null && !W.scanning) scan();

    } else if (W.step === 2) {
      const nameIn = el("input", { type: "text", value: W.name });
      nameIn.addEventListener("input", () => { W.name = nameIn.value; });
      const ctxLabel = () => (W.metaKnown && W.ctx === W.ctxMax ? "model max" : "");
      const ctxVal = el("span", { class: "mods-hint", text: ctxLabel() });
      const slider = el("input", { type: "range", class: "wiz-slider",
        min: "2048", max: String(W.ctxMax), step: "1024", value: String(W.ctx) });
      // precise entry: the number box and the slider drive the same value
      const ctxNum = el("input", { type: "number", class: "wiz-ctxnum",
        min: "2048", max: String(W.ctxMax), step: "1024", value: String(W.ctx) });
      const setCtx = (v, from) => {
        W.ctx = Math.max(2048, Math.min(W.ctxMax, Math.round(Number(v) || 2048)));
        if (from !== "slider") slider.value = String(W.ctx);
        if (from !== "num") ctxNum.value = String(W.ctx);
        ctxVal.textContent = ctxLabel();
      };
      slider.addEventListener("input", () => setCtx(slider.value, "slider"));
      ctxNum.addEventListener("input", () => {
        const v = Number(ctxNum.value);
        if (Number.isFinite(v) && v >= 2048) setCtx(v, "num");
      });
      ctxNum.addEventListener("blur", () => setCtx(ctxNum.value, "blur"));
      const beSel = el("select", { class: "term-sel" });
      for (const b of WIZ_BACKENDS) beSel.append(el("option", { value: b, text: b }));
      beSel.value = W.backend;
      beSel.addEventListener("change", () => { W.backend = beSel.value; });
      const binIn = el("input", { type: "text", value: W.binary,
        title: "llama-server binary name/path on the host — point at a backend-specific build if you keep several" });
      binIn.addEventListener("input", () => { W.binary = binIn.value.trim() || "llama-server"; });
      const mmRow = el("div", { class: "wiz-mmrow" });
      if (W.mmproj) {
        const chk = el("input", { type: "checkbox" });
        chk.checked = W.useMm;
        chk.addEventListener("change", () => { W.useMm = chk.checked; });
        mmRow.append(el("label", { class: "chk" }, chk,
          "Use the vision projector found beside it (" + baseName(W.mmproj) + ")"));
      }
      body.append(
        el("p", { class: "wiz-hint", text: (W.host || "localhost") + "  ·  " + W.sel }),
        el("div", { class: "ts-row" }, el("label", { text: "Name" }), nameIn),
        el("div", { class: "ts-row" }, el("label", { text: "Context" }),
          el("div", { style: "width:100%" }, slider,
            el("div", { class: "wiz-ctxval" }, ctxNum, ctxVal,
              W.metaKnown ? null : el("span", { class: "mods-hint",
                text: "  trained size unknown — capped at 131,072" })))),
        el("div", { class: "ts-row" }, el("label", { text: "Backend" }), beSel),
        el("div", { class: "ts-row" }, el("label", { text: "Binary" }), binIn),
        mmRow,
        el("div", { class: "wiz-nav" },
          el("button", { class: "btn", text: "← Pick another",
            onclick: () => { W.step = 1; render(); } }),
          el("span", { class: "spacer" }),
          setHotkey(el("button", { class: "btn btn-acc", text: "Review →",
            disabled: W.name.trim() ? null : "",
            onclick: async () => {
              W.step = 3;
              render();
              const r = await Api.call("models_yaml", [entry()]);
              W.snippet = r.ok ? r.data.yaml : "error: " + r.error;
              render();
            } }), "Enter")));

    } else {
      // the snippet is EDITABLE in the real library editor (yaml syntax
      // highlighting) — whatever you make of it is exactly what lands in
      // loom.yaml (validated before writing)
      const host = el("div", { class: "wiz-yaml-host" });
      body.append(
        el("p", { class: "wiz-hint", text: "These exact lines land at the end of the models: block — comments and ordering elsewhere stay untouched." }),
        host,
        el("div", { class: "wiz-nav" },
          el("button", { class: "btn", text: "← Back (regenerates)",
            onclick: () => {
              W.editor?.destroy();
              W.editor = null;
              W.step = 2;
              W.snippet = "";
              render();
            } }),
          el("span", { class: "spacer" }),
          el("button", { class: "btn", text: "Copy",
            onclick: () => {
              copyText(W.editor ? W.editor.getValue() : W.snippet || "");
              toast("Copied.", "ok", 1500);
            } }),
          setHotkey(el("button", { class: "btn btn-acc", text: "Add to loom.yaml",
            title: "Ctrl+Enter also works from inside the editor",
            onclick: async () => {
              const text = W.editor ? W.editor.getValue() : W.snippet || "";
              const r = await Api.call("config_add_model_text", text);
              if (!r.ok) { toast(r.error, "err"); return; }
              toast("Added to loom.yaml", "ok");
              W.editor?.destroy();
              W.editor = null;
              closeTopModal();
              refreshServersTab();
              // the Library editor may be SHOWING loom.yaml — reload it
              libReloadIfOpen(st.lib.configFile || "loom.yaml");
            } }), "Ctrl+Enter")));
      W.editor?.destroy();
      W.editor = new LoomEditor(host, {
        text: W.snippet || "", mode: "code", lang: "yaml",
      });
    }

    // keyboard: focus follows the step — the filter on 1, the name on 2,
    // Add on 3. Async re-renders (scan results, gguf meta) rebuild the
    // DOM, so this also restores where the user was typing.
    setTimeout(() => {
      if (!body.isConnected) return;
      if (W.step === 3) {
        body.querySelector(".wiz-nav .btn-acc")?.focus();
        return;
      }
      const f = body.querySelector("input[type='text']");
      if (f) {
        f.focus();
        f.setSelectionRange(f.value.length, f.value.length);
      }
    }, 0);
  }
  render();
}
