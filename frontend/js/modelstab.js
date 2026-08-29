/* modelstab.js — the Models utility (singleton tab): manage GGUFs across
 * machines. Top to bottom: the fetcher (URL / HF repo → quant picker;
 * a repo's mmproj vision projector downloads automatically alongside the
 * quant you pick), "+ add ssh host…", then a tab strip — Local first
 * (pinned), one tab per ssh host (drag to reorder, right-click to
 * forget). Each tab auto-scans the common model folders on first open,
 * caches the result, and has a reload button + filter. Rows offer
 * "Server…" (the New-model wizard prefilled with this host + file) and,
 * on Local, "Copy to…" any ssh host's ~/.loom/models. */
"use strict";

function modelsState() {
  return st.modelsTab || (st.modelsTab = {
    hosts: [], active: "", tabs: {}, jobs: {},
  });
}

/* per-host-tab state; key "" = Local */
function modelsHostTab(key) {
  const ms = modelsState();
  return ms.tabs[key] || (ms.tabs[key] = {
    entries: null, scanning: false, filter: "",
  });
}

function mountModelsTab(panel) {
  panel.classList.add("modstab");
  refreshModelsTab();
}

async function refreshModelsTab() {
  const ms = modelsState();
  const res = await Api.call("models_hosts");
  if (res.ok) {
    ms.hosts = res.data.hosts;
    for (const k of Object.keys(ms.tabs)) {
      if (k && !ms.hosts.includes(k)) delete ms.tabs[k];
    }
    if (ms.active && !ms.hosts.includes(ms.active)) ms.active = "";
  }
  renderModelsTab();
  scanModelsHost(ms.active);
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  return Math.round(n / 1024) + " KB";
}

async function scanModelsHost(key, force) {
  const t = modelsHostTab(key);
  if (t.scanning || (t.entries !== null && !force)) return;
  t.scanning = true;
  renderModelsTab();
  const r = await Api.call("models_scan", key);
  t.scanning = false;
  if (!r.ok) {
    toast(r.error, "err");
    if (t.entries === null) t.entries = [];
  } else {
    t.entries = r.data.entries;
  }
  renderModelsTab();
}

function activateModelsHost(key) {
  const ms = modelsState();
  ms.active = key;
  renderModelsTab();
  scanModelsHost(key);
}

let _dragModelsHost = null;

function renderModelsTab() {
  const panel = panelFor("models");
  if (!panel) return;
  const ms = modelsState();
  panel.replaceChildren();
  const wrap = el("div", { class: "mods-wrap" });

  wrap.append(el("div", { class: "srv-head" }, el("h2", { text: "Models" })));
  wrap.append(el("p", { class: "mods-hint",
    text: "Fetch GGUFs here, then browse each machine's models below — copy them from Local to your ssh hosts, or turn any of them into a server." }));

  /* ---- the fetcher: direct .gguf URL, or a repo → pick the quant ---- */
  const startDownload = async (u, label) => {
    const r = await Api.call("models_download", u, "");
    if (!r.ok) { toast(r.error, "err"); return false; }
    ms.jobs[r.data.job] = { op: "download", label: label || baseName(u), done: 0, total: 0 };
    renderModelsTab();
    return true;
  };
  const urlIn = el("input", { type: "text", class: "mods-url", value: ms.dlInput || "",
    placeholder: "owner/repo (e.g. unsloth/Qwen3.8-27B-GGUF), a repo URL, or a direct .gguf URL" });
  urlIn.addEventListener("input", () => { ms.dlInput = urlIn.value; });
  const dlBtn = el("button", {
    class: "btn btn-sm btn-acc", text: ms.fetching ? "Fetching…" : "Fetch",
    disabled: ms.fetching ? "" : null,
    onclick: async () => {
      const u = urlIn.value.trim();
      if (!u) return;
      if (/\.gguf(\?.*)?$/i.test(u)) {          // direct file → just download
        ms.dlInput = "";
        startDownload(u);
        return;
      }
      ms.fetching = true;
      renderModelsTab();
      const r = await Api.call("models_repo", u);
      ms.fetching = false;
      if (!r.ok) { toast(r.error, "err"); renderModelsTab(); return; }
      ms.repoFiles = r.data.files;
      ms.repoName = u;
      ms.repoMmDone = false;
      renderModelsTab();
    },
  });
  urlIn.addEventListener("keydown", (e) => { if (e.key === "Enter") dlBtn.click(); });
  wrap.append(el("div", { class: "mods-dl" }, urlIn, dlBtn));

  if (ms.repoFiles?.length) {
    const mm = repoMmproj(ms.repoFiles);
    const rl = el("div", { class: "mods-list" },
      el("div", { class: "mods-dir" },
        el("span", { text: "quants in " + ms.repoName + "  " }),
        el("button", { class: "btn btn-sm", text: "×", title: "Clear",
          onclick: () => { ms.repoFiles = null; renderModelsTab(); } })));
    if (mm) {
      rl.append(el("div", { class: "mods-dir" },
        el("span", { text: "vision projector detected (" + baseName(mm.path)
          + ") — it downloads automatically with the quant you pick" })));
    }
    for (const f of ms.repoFiles) {
      const isMm = /mmproj/i.test(baseName(f.path));
      rl.append(el("div", { class: "mods-row" + (isMm ? " mm" : "") },
        el("span", { class: "pname", title: f.path, text: f.path }),
        isMm ? el("span", { class: "mods-badge", text: "mmproj" }) : null,
        el("span", { class: "mods-prog", text: fmtBytes(f.size) }),
        el("button", {
          class: "btn btn-sm btn-acc", text: "Download",
          onclick: async () => {
            const ok = await startDownload(f.url, baseName(f.path));
            // the paired projector rides along, once per repo fetch
            if (ok && !isMm && mm && !ms.repoMmDone) {
              ms.repoMmDone = true;
              startDownload(mm.url, baseName(mm.path));
            }
          },
        })));
    }
    wrap.append(rl);
  }

  /* ---- jobs ---- */
  const jobIds = Object.keys(ms.jobs);
  if (jobIds.length) {
    const jl = el("div", { class: "mods-jobs" });
    for (const jid of jobIds) {
      const j = ms.jobs[jid];
      const pct = j.total ? Math.round(100 * j.done / j.total) : null;
      jl.append(el("div", { class: "mods-job" },
        el("span", { class: "pname",
          text: (j.op === "push" ? "→ " + (j.host || "") + "  " : "⇣ ") + j.label }),
        el("span", { class: "mods-prog", text: j.error ? ("failed: " + j.error)
          : j.finished ? "done"
          : pct != null ? `${pct}%  (${fmtBytes(j.done)} / ${fmtBytes(j.total)})`
          : fmtBytes(j.done) }),
        (!j.finished && !j.error) ? el("button", {
          class: "btn btn-sm", text: "Cancel",
          onclick: () => Api.call("models_cancel", jid),
        }) : el("button", {
          class: "btn btn-sm", text: "×",
          onclick: () => { delete ms.jobs[jid]; renderModelsTab(); },
        })));
    }
    wrap.append(jl);
  }

  /* ---- add host ---- */
  wrap.append(el("div", { class: "mods-hosts" }, el("button", {
    class: "btn btn-sm", text: "+ add ssh host…",
    onclick: () => promptModal("Add a model host",
      "ssh destination (user@host or a ~/.ssh/config alias):", "",
      async (v) => {
        const dest = v.trim();
        if (!dest) return;
        const r = await Api.call("models_host_add", dest);
        if (!r.ok) { toast(r.error, "err"); return; }
        ms.hosts = r.data.hosts;
        activateModelsHost(dest);
      }, "Add"),
  })));

  /* ---- host tabs: Local pinned first, ssh hosts drag-reorderable ---- */
  const strip = el("div", { class: "mods-tabs" });
  const mkTab = (key, label) => {
    const node = el("div", {
      class: "mods-tab" + (ms.active === key ? " active" : ""),
      draggable: key ? "true" : null,
      title: key ? "drag to reorder · right-click to forget" : "this machine",
      onclick: () => activateModelsHost(key),
      oncontextmenu: (e) => {
        e.preventDefault();
        const items = [{ label: "Rescan", fn: () => scanModelsHost(key, true) }];
        if (key) {
          items.push({ label: "Forget host", fn: async () => {
            const r = await Api.call("models_host_remove", key);
            if (!r.ok) { toast(r.error, "err"); return; }
            ms.hosts = r.data.hosts;
            delete ms.tabs[key];
            if (ms.active === key) ms.active = "";
            renderModelsTab();
          } });
        }
        ctxMenu(e.clientX, e.clientY, items);
      },
    }, el("span", { text: label }));
    if (!key) return node;
    // same drop-on-target reorder dance as the main tab strip
    node.addEventListener("dragstart", (e) => {
      _dragModelsHost = key;
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", key); } catch (err) { /* ok */ }
    });
    node.addEventListener("dragover", (e) => {
      if (!_dragModelsHost || _dragModelsHost === key) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      node.classList.add("drop-target");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-target"));
    node.addEventListener("drop", async (e) => {
      e.preventDefault();
      node.classList.remove("drop-target");
      const from = _dragModelsHost;
      _dragModelsHost = null;
      const order = ms.hosts.filter((h) => h !== from);
      order.splice(order.indexOf(key), 0, from);
      const r = await Api.call("models_hosts_reorder", order);
      if (!r.ok) { toast(r.error, "err"); refreshModelsTab(); return; }
      ms.hosts = r.data.hosts;
      renderModelsTab();
    });
    node.addEventListener("dragend", () => {
      _dragModelsHost = null;
      for (const n of strip.querySelectorAll(".drop-target")) {
        n.classList.remove("drop-target");
      }
    });
    return node;
  };
  strip.append(mkTab("", "Local"));
  for (const h of ms.hosts) strip.append(mkTab(h, h));
  wrap.append(strip);

  /* ---- the active host's models: reload + filter + rows ---- */
  const t = modelsHostTab(ms.active);
  const bar = el("div", { class: "mods-dl" });
  const filterIn = el("input", { type: "text", class: "mods-url", value: t.filter,
    placeholder: "filter — space-separated words, all must match" });
  bar.append(filterIn, el("button", {
    class: "btn btn-sm", title: "Rescan " + (ms.active || "this machine"),
    html: icon("refresh", 14), disabled: t.scanning ? "" : null,
    onclick: () => scanModelsHost(ms.active, true),
  }));
  wrap.append(bar);

  const list = el("div", { class: "mods-list" });
  const renderList = () => {
    list.replaceChildren();
    if (t.entries === null) {
      list.append(el("div", { class: "picker-empty",
        text: t.scanning ? "Scanning…" : "Not scanned yet." }));
      return;
    }
    const terms = t.filter.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = (t.entries || []).filter((e2) =>
      terms.every((tm) => e2.path.toLowerCase().includes(tm)));
    if (!rows.length) {
      list.append(el("div", { class: "picker-empty",
        text: t.entries.length ? "Nothing matches the filter." : "No .gguf files found." }));
    }
    const vision = new Set((t.entries || [])
      .filter((e2) => e2.mmproj && e2.pairedWith).map((e2) => e2.pairedWith));
    for (const e2 of rows) {
      const row = el("div", { class: "mods-row" + (e2.mmproj ? " mm" : "") },
        el("span", { class: "pname", title: e2.path, text: e2.path }),
        e2.mmproj ? el("span", { class: "mods-badge", text: "mmproj" })
          : vision.has(e2.path) ? el("span", { class: "mods-badge ok", text: "vision" }) : null,
        el("span", { class: "mods-size", text: fmtBytes(e2.size) }));
      if (!e2.mmproj) {
        row.append(el("button", {
          class: "btn btn-sm btn-acc", text: "Server…",
          title: "Configure a llama-server for this model (New-model wizard)",
          onclick: () => modelWizard({ host: ms.active, path: e2.path }),
        }));
      }
      if (!ms.active && ms.hosts.length) {
        row.append(el("button", {
          class: "btn btn-sm", text: "Copy to…",
          title: "Copy this file to a host's ~/.loom/models",
          onclick: (ev2) => {
            ctxMenu(ev2.clientX, ev2.clientY, ms.hosts.map((h) => ({
              label: "→ " + h,
              fn: async () => {
                const r = await Api.call("models_push", e2.path, h);
                if (!r.ok) { toast(r.error, "err"); return; }
                ms.jobs[r.data.job] = { op: "push", host: h,
                  label: baseName(e2.path), done: 0, total: 0 };
                renderModelsTab();
              },
            })));
          },
        }));
      }
      list.append(row);
    }
  };
  filterIn.addEventListener("input", () => { t.filter = filterIn.value; renderList(); });
  wrap.append(list);
  renderList();
  panel.append(wrap);
}

/* the repo's vision projector, if it ships one: prefer the F16 build,
 * else the smallest (F32/BF16 waste disk for no quality gain here) */
function repoMmproj(files) {
  const mms = (files || []).filter((f) => /mmproj/i.test(baseName(f.path)));
  if (!mms.length) return null;
  return mms.find((f) => /f16/i.test(baseName(f.path)))
    || mms.slice().sort((a, b) => a.size - b.size)[0];
}

/* progress events */
function onModelsEvent(ev) {
  const ms = modelsState();
  const j = ms.jobs[ev.id] || (ms.jobs[ev.id] = { op: ev.op, label: ev.name || "", done: 0, total: 0 });
  if (ev.kind === "progress") {
    j.label = ev.name || j.label;
    j.host = ev.host || j.host;
    j.done = ev.done || 0;
    j.total = ev.total || 0;
  } else if (ev.kind === "done") {
    j.finished = true;
    toast((ev.op === "push" ? "Pushed " : "Downloaded ") + (ev.name || "")
      + (ev.host ? " → " + ev.host : "") + " (" + (ev.path || "") + ")", "ok");
    // a finished download lands in ~/.loom/models — reflect it on Local;
    // a finished push lands on its host's tab
    const key = ev.op === "push" ? (ev.host || null) : "";
    const tab = key === null ? null : ms.tabs[key];
    if (tab && tab.entries !== null) scanModelsHost(key, true);
  } else if (ev.kind === "error") {
    j.error = ev.msg || "failed";
    toast((ev.op === "push" ? "Push" : "Download") + " failed: " + (ev.msg || ""), "err");
  }
  if (st.activeTab === "models") renderModelsTab();
}
