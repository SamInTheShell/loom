/* downloader.js — the Downloader (singleton tab, opened by the
 * "Downloader" button on the Models tab): fetch GGUFs into
 * ~/.loom/models. Top: URL / HF repo input → quant picker (split ggufs
 * are ONE quant with an "N parts" badge; a repo's mmproj projector rides
 * along automatically). Below: the download list — every download is a
 * persistent backend record with pause/resume (HTTP Range on the .part),
 * so an app crash or reboot resumes instead of restarting hundreds of
 * GB. Records live in the backend; this tab is just a view of them. */
"use strict";

function dlState() {
  return st.dl || (st.dl = {
    input: "", fetching: false,
    repoFiles: null, repoName: "", repoMmDone: false,
    records: null,
  });
}

function mountDownloaderTab(panel) {
  panel.classList.add("modstab");
  refreshDownloaderTab();
}

async function refreshDownloaderTab() {
  const ds = dlState();
  const r = await Api.call("models_downloads");
  if (r.ok) ds.records = r.data.downloads;
  else if (ds.records === null) ds.records = [];
  renderDownloaderTab();
}

function renderDownloaderTab() {
  const panel = panelFor("downloader");
  if (!panel) return;
  const ds = dlState();
  const focus = captureFocus(panel);
  panel.replaceChildren();
  const wrap = el("div", { class: "mods-wrap" });

  wrap.append(el("div", { class: "srv-head" }, el("h2", { text: "Downloader" })));
  wrap.append(el("p", { class: "mods-hint",
    text: "Fetch GGUFs into ~/.loom/models — paste a Hugging Face repo to pick "
      + "a quantization, or a direct .gguf URL. Downloads survive restarts: "
      + "pause and resume at will." }));

  /* ---- the fetcher: direct .gguf URL, or a repo → pick the quant ---- */
  const startDownload = async (u, label, total) => {
    const r = await Api.call("models_download", u, "", total || 0);
    if (!r.ok) { toast(r.error, "err"); return false; }
    refreshDownloaderTab();
    return true;
  };
  const urlIn = el("input", { type: "text", class: "mods-url", "data-keep": "dl-url",
    value: ds.input || "",
    placeholder: "owner/repo (e.g. unsloth/Qwen3.8-27B-GGUF), a repo URL, or a direct .gguf URL" });
  urlIn.addEventListener("input", () => { ds.input = urlIn.value; });
  const dlBtn = el("button", {
    class: "btn btn-sm btn-acc", text: ds.fetching ? "Fetching…" : "Fetch",
    disabled: ds.fetching ? "" : null,
    onclick: async () => {
      const u = urlIn.value.trim();
      if (!u) return;
      if (/\.gguf(\?.*)?$/i.test(u)) {          // direct file → just download
        ds.input = "";
        startDownload(u);
        return;
      }
      ds.fetching = true;
      renderDownloaderTab();
      const r = await Api.call("models_repo", u);
      ds.fetching = false;
      if (!r.ok) { toast(r.error, "err"); renderDownloaderTab(); return; }
      ds.repoFiles = r.data.files;
      ds.repoName = u;
      ds.repoMmDone = false;
      renderDownloaderTab();
    },
  });
  urlIn.addEventListener("keydown", (e) => { if (e.key === "Enter") dlBtn.click(); });
  wrap.append(el("div", { class: "mods-dl" }, urlIn, dlBtn));

  if (ds.repoFiles?.length) {
    const mm = repoMmproj(ds.repoFiles);
    const rl = el("div", { class: "mods-list" },
      el("div", { class: "mods-dir" },
        el("span", { text: "quants in " + ds.repoName + "  " }),
        el("button", { class: "btn btn-sm", text: "×", title: "Clear",
          onclick: () => { ds.repoFiles = null; renderDownloaderTab(); } })));
    if (mm) {
      rl.append(el("div", { class: "mods-dir" },
        el("span", { text: "vision projector detected (" + baseName(mm.path)
          + ") — it downloads automatically with the quant you pick" })));
    }
    for (const f of ds.repoFiles) {
      const isMm = /mmproj/i.test(baseName(f.path));
      rl.append(el("div", { class: "mods-row" + (isMm ? " mm" : "") },
        el("span", { class: "pname", title: f.path, text: f.path }),
        isMm ? el("span", { class: "mods-badge", text: "mmproj" }) : null,
        f.parts ? el("span", { class: "mods-badge",
          title: "split gguf — all parts download together",
          text: f.parts.length + " parts" }) : null,
        el("span", { class: "mods-prog", text: fmtBytes(f.size) }),
        el("button", {
          class: "btn btn-sm btn-acc", text: "Download",
          onclick: async () => {
            const ok = await startDownload(
              f.parts ? f.parts.map((p) => p.url) : f.url,
              baseName(f.path), f.size);
            // the paired projector rides along, once per repo fetch
            if (ok && !isMm && mm && !ds.repoMmDone) {
              ds.repoMmDone = true;
              startDownload(mm.url, baseName(mm.path), mm.size);
            }
          },
        })));
    }
    wrap.append(rl);
  }

  /* ---- the persistent download records: live first, then history.
   * History (finished downloads) is retained until the user removes an
   * item (×) or clears the whole list. ---- */
  const recs = ds.records;
  const api = (op, id) => async () => {
    const r = await Api.call(op, id);
    if (!r.ok) { toast(r.error, "err"); return; }
    refreshDownloaderTab();
  };
  const row = (rec) => {
    const pct = rec.total ? Math.round(100 * (rec.done || 0) / rec.total) : null;
    const bytes = fmtBytes(rec.done || 0)
      + (rec.total ? " / " + fmtBytes(rec.total) : "");
    const prog =
      rec.status === "done" ? fmtBytes(rec.done || 0)
      : rec.status === "error" ? "failed: " + (rec.error || "error")
        + "  (" + bytes + " kept)"
      : rec.status === "paused" ? "paused — " + bytes
      : pct != null ? pct + "%  (" + bytes + ")"
      : bytes;
    const node = el("div", { class: "mods-job" },
      el("span", { class: "pname", title: (rec.names || []).join("\n"),
        text: "⇣ " + (rec.label || "") }),
      rec.names?.length > 1 ? el("span", { class: "mods-badge",
        text: rec.names.length + " parts" }) : null,
      el("span", { class: "mods-prog", text: prog }),
      rec.status === "done" && rec.doneTs
        ? el("span", { class: "mods-prog" }, tago(rec.doneTs)) : null);
    if (rec.status === "active") {
      node.append(el("button", { class: "btn btn-sm", text: "Pause",
        onclick: api("models_dl_pause", rec.id) }));
    }
    if (rec.status === "paused" || rec.status === "error") {
      node.append(el("button", { class: "btn btn-sm btn-acc", text: "Resume",
        onclick: api("models_dl_resume", rec.id) }));
    }
    if (rec.status === "done") {
      node.append(el("button", { class: "btn btn-sm", text: "×",
        title: "Remove from history (the files stay)",
        onclick: api("models_dl_dismiss", rec.id) }));
    } else {
      node.append(el("button", { class: "btn btn-sm", text: "Cancel",
        title: "Abort and DELETE the downloaded bytes",
        onclick: api("models_dl_cancel", rec.id) }));
    }
    return node;
  };
  const live = (recs || []).filter((r) => r.status !== "done");
  const hist = (recs || []).filter((r) => r.status === "done")
    .sort((a, b) => (b.doneTs || b.ts || 0) - (a.doneTs || a.ts || 0));
  const jl = el("div", { class: "mods-jobs" });
  if (recs === null) {
    jl.append(el("div", { class: "picker-empty", text: "Loading…" }));
  } else if (!recs.length) {
    jl.append(el("div", { class: "picker-empty", text: "No downloads yet." }));
  }
  for (const rec of live) jl.append(row(rec));
  if (hist.length) {
    jl.append(el("div", { class: "mods-histhead" },
      el("span", { text: "history" }),
      el("button", { class: "btn btn-sm", text: "Clear history",
        style: "margin-left:auto",
        title: "Forget all finished downloads — the files stay",
        onclick: api("models_dl_clear") })));
    for (const rec of hist) jl.append(row(rec));
  }
  wrap.append(jl);
  panel.append(wrap);
  restoreFocus(panel, focus);
}

/* download events (op:"download") — routed here from main.js. The tab
 * may be closed: records are backend truth, so just patch our copy if we
 * have one and repaint when visible; toasts fire regardless. */
function onDownloaderEvent(ev) {
  const ds = dlState();
  const rec = (ds.records || []).find((r) => r.id === ev.id);
  if (ev.kind === "progress") {
    if (rec) {
      rec.status = "active";
      rec.done = ev.done || 0;
      if (ev.total) rec.total = ev.total;
    }
  } else if (ev.kind === "paused") {
    if (rec) { rec.status = "paused"; rec.done = ev.done || rec.done; }
  } else if (ev.kind === "done") {
    if (rec) {
      rec.status = "done";
      rec.done = ev.size || rec.done;
      rec.doneTs = rec.doneTs || Date.now();
    }
    toast("Downloaded " + (ev.name || "") + " (" + (ev.path || "") + ")", "ok");
    // the finished model lands in ~/.loom/models — reflect it on the
    // Models tab's Local list if that list has been loaded
    const tab = st.modelsTab?.tabs?.[""];
    if (tab && tab.entries !== null) scanModelsHost("", true);
  } else if (ev.kind === "cancelled") {
    if (ds.records) ds.records = ds.records.filter((r) => r.id !== ev.id);
  } else if (ev.kind === "error") {
    if (rec) { rec.status = "error"; rec.error = ev.msg || "failed"; }
    toast("Download failed: " + (ev.msg || "") + " — resumable in the Downloader", "err");
  }
  if (st.activeTab === "downloader") renderDownloaderTab();
}
