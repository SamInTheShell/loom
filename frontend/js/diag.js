/* diag.js - the chat diagnostics tab, built to be immediately
 * informative without hunting:
 *
 *   ┌ toolbar: title · model · metric picker · legend · zoom · export ┐
 *   ├ summary tiles: the session's headline numbers (groups with no    │
 *   │   data vanish whole - never a dash)                              │
 *   ├ graph: per-entry bars, FIXED - it never scrolls away             │
 *   └ table: the ledger, scrolling in its OWN region                   ┘
 *
 * The graph and table stay correlated in both directions: clicking a
 * bar jumps the table (and pulses both), clicking a row pans the graph.
 * ↑/↓ anywhere in the tab walk the selection through the entries;
 * ←/→/+/−/Home/End drive the graph. Real server-reported numbers
 * (tok/s, prefill speed, cache reuse, TTFT, true context size) are
 * preferred everywhere; chars/4 estimates fill the gaps and are marked
 * with ~. */
"use strict";

const DIAG_KINDS = {
  user: { label: "user", color: "--run" },
  assistant: { label: "model", color: "--ok" },
  think: { label: "thoughts", color: null, fallback: "#a78bfa" },
  tool: { label: "tools", color: "--warn" },
  // bright blood red - compaction must jump out of the ledger
  compact: { label: "compaction", color: null, fallback: "#e8112d" },
};

const DIAG_METRICS = {
  tokens: { label: "Tokens", value: (r) => r.tokens || 0,
            fmt: (x) => fmtTok(x) },
  tps: { label: "Tok/s",
         value: (r) => (r.real?.tps
           || (r.durMs > 0 ? (r.tokens || 0) / (r.durMs / 1000) : 0)),
         fmt: (x) => (x >= 100 ? Math.round(x) : x.toFixed(1)) + "/s" },
  dur: { label: "Duration", value: (r) => r.durMs || 0,
         fmt: (x) => fmtDur(Math.round(x)) },
  ctx: { label: "Context", value: (r) => r.ctxAt || 0,
         fmt: (x) => fmtTok(x) },
};

function diagTabTitle(chatId) {
  const t = st.chats[chatId]?.chat?.title;
  return "Diag · " + (t || "chat");
}

function openDiagTab(chatId) {
  openTab("diag", chatId);
}

function fmtDur(ms) {
  ms = Number(ms) || 0;
  if (ms <= 0) return "·";
  if (ms < 1000) return ms + "ms";
  if (ms < 90_000) return (ms / 1000).toFixed(1) + "s";
  return Math.round(ms / 60000) + "m" + Math.round((ms % 60000) / 1000) + "s";
}

function mountDiagTab(panel, chatId) {
  panel.classList.add("diagtab");
  panel.append(el("div", { class: "picker-empty", text: "Loading…" }));
  Api.call("chat_diag", chatId).then((res) => {
    if (!res.ok) {
      panel.replaceChildren(el("div", { class: "picker-empty", text: res.error }));
      return;
    }
    buildDiagView(panel, chatId, res.data);
  });
}

/* ---------- live refresh ----------
 * The view re-pulls chat_diag whenever the chat lands new data (each
 * turn, tool result, compaction - chat.js calls this from its refresh
 * path, the popped-out window from its own event listener). Debounced;
 * the rebuild PRESERVES the metric, zoom, pan, and selection - and
 * keeps following the tail when the user was on the newest entry. */
const _diagRefreshTimers = {};

function refreshDiagView(chatId) {
  clearTimeout(_diagRefreshTimers[chatId]);
  _diagRefreshTimers[chatId] = setTimeout(async () => {
    const v = st._diagViews?.[chatId];
    if (!v || !v.host?.isConnected) return;
    const res = await Api.call("chat_diag", chatId);
    if (!res.ok) return;   // transient (chat mid-save) - next event retries
    const cur = st._diagViews?.[chatId];
    if (!cur || !cur.host?.isConnected) return;
    buildDiagView(cur.host, chatId, res.data, cur);
  }, 400);
}

function diagColor(kind) {
  const k = DIAG_KINDS[kind] || DIAG_KINDS.tool;
  if (k.color) {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(k.color).trim();
    if (v) return v;
  }
  return k.fallback || "#888";
}

/* ---------- the session fold: sums and counts, averaged at render ----------
 * Ratio-of-sums everywhere (Σtokens ÷ Σtime), never mean-of-ratios -
 * short turns must not skew the speeds. Entries missing a figure simply
 * stay out of that figure's denominator. */
function diagSummary(rows) {
  const s = { turns: 0, entries: rows.length,
              inTok: 0, outTok: 0, estTok: 0,
              llmMs: 0, toolMs: 0,
              genMs: 0, genTok: 0,
              ppMs: 0, ppTok: 0,
              cacheN: 0, promptTotal: 0,
              ttftMs: 0, ttftN: 0,
              lastCtx: 0 };
  for (const r of rows) {
    s.estTok += r.tokens || 0;
    if (r.kind === "user") s.turns++;
    if (r.kind === "tool") s.toolMs += r.durMs || 0;
    if (r.kind === "assistant" || r.kind === "think") s.llmMs += r.durMs || 0;
    const re = r.kind === "assistant" ? r.real : null;
    if (!re) continue;
    if (re.outN) { s.outTok += re.outN; }
    if (re.genMs && re.outN) { s.genMs += re.genMs; s.genTok += re.outN; }
    if (re.promptMs && re.promptN) { s.ppMs += re.promptMs; s.ppTok += re.promptN; }
    if (re.promptN || re.cacheN) {
      s.cacheN += re.cacheN || 0;
      s.promptTotal += (re.cacheN || 0) + (re.promptN || 0);
      s.inTok += re.promptN || 0;   // freshly processed prompt tokens
    }
    if (re.ttftMs) { s.ttftMs += re.ttftMs; s.ttftN++; }
    if (re.ctxTokens) s.lastCtx = re.ctxTokens;
  }
  return s;
}

/* summary tiles - each group renders only when it has real data */
function diagTiles(rows, nCtx) {
  const s = diagSummary(rows);
  const tile = (label, value, title) => el("div", { class: "diag-tile", title },
    el("b", { text: value }), el("span", { text: label }));
  const tiles = [];
  tiles.push(tile("turns · entries", s.turns + " · " + s.entries,
    "User turns in this chat, and entries in the ledger below"));
  if (s.llmMs || s.toolMs) {
    tiles.push(tile("model · tools",
      fmtDur(Math.round(s.llmMs)) + " · " + fmtDur(Math.round(s.toolMs)),
      "Wall time spent generating vs. running tool calls"));
  }
  if (s.genTok && s.genMs) {
    tiles.push(tile("gen tok/s",
      (s.genTok / (s.genMs / 1000)).toFixed(1),
      "Generation speed across the whole chat - total output tokens ÷ "
      + "total generation time (server-reported)"));
  }
  if (s.ppTok && s.ppMs) {
    tiles.push(tile("prefill tok/s",
      String(Math.round(s.ppTok / (s.ppMs / 1000))),
      "Prompt processing speed - freshly processed prompt tokens ÷ "
      + "prefill time (cached tokens are free and excluded)"));
  }
  if (s.ttftN) {
    tiles.push(tile("avg ttft",
      (s.ttftMs / s.ttftN / 1000).toFixed(2) + "s",
      "Average time to first token, over the " + s.ttftN
      + " turn(s) that recorded one"));
  }
  if (s.promptTotal) {
    tiles.push(tile("cache hit",
      Math.round(100 * s.cacheN / s.promptTotal) + "%",
      "How much of the prompt work the server's KV cache absorbed - "
      + fmtTok(s.cacheN) + " of " + fmtTok(s.promptTotal)
      + " prompt tokens were reused instead of reprocessed"));
  }
  if (s.outTok) {
    tiles.push(tile("output tok", fmtTok(s.outTok),
      "Total tokens generated (thinking + messages + tool calls, "
      + "server-reported)"));
  } else if (s.estTok) {
    tiles.push(tile("~total tok", fmtTok(s.estTok),
      "Estimated total tokens (chars ÷ 4 - no server-reported usage yet)"));
  }
  if (s.lastCtx) {
    tiles.push(tile("context",
      fmtTok(s.lastCtx) + (nCtx ? " / " + fmtTok(nCtx)
        + " (" + Math.round(100 * s.lastCtx / nCtx) + "%)" : ""),
      "Context after the last completed turn (server-reported), "
      + (nCtx ? "against the model's window" : "window size unknown")));
  }
  return el("div", { class: "diag-tiles" }, ...tiles);
}

function buildDiagView(panel, chatId, data, prev) {
  // a REBUILD (live refresh) carries the old view's state over: what
  // the user zoomed/panned/selected must survive new data arriving.
  // "Following the tail" (selection on the newest entry - the default)
  // keeps following; an explicit selection elsewhere stays put.
  const follow = !prev || prev.sel < 0
    || prev.sel >= (prev.rows?.length || 0) - 1;
  const oldTable = prev ? panel.querySelector(".diag-table") : null;
  const oldTableScroll = oldTable ? oldTable.scrollTop : 0;
  // scroll-to-bottom happens on OPEN, and on refresh only when the user
  // was already AT the bottom - a mid-scroll reader is never yanked back
  const wasAtBottom = !oldTable || !oldTable.clientHeight
    || oldTable.scrollTop + oldTable.clientHeight
       >= oldTable.scrollHeight - 40;
  const hadFocus = prev && panel.contains(document.activeElement);
  prev?._abort?.abort();   // the old build's window-level listeners
  const ac = new AbortController();
  panel.replaceChildren();
  const rows = data.rows || [];
  // cumulative real context per entry (for the Context graph metric):
  // assistant rows carry the truth; other rows inherit the last known
  let ctx = 0;
  for (const r of rows) {
    if (r.kind === "assistant" && r.real?.ctxTokens) ctx = r.real.ctxTokens;
    r.ctxAt = ctx;
  }

  const v = {
    rows,
    host: panel,
    metric: prev?.metric || "tokens",  // tokens | tps | dur | ctx
    maxVal: 1,
    scale: prev?.scale || 10,   // px per bar
    offset: prev?.offset || 0,  // first visible bar (float, in bar units)
    sel: -1,
    flashT: 0,        // performance.now() of the last selection (pulse)
    _abort: ac,       // tears down this build's window-level listeners
  };
  st._diagViews = st._diagViews || {};
  st._diagViews[chatId] = v;

  function metricOf(r) { return DIAG_METRICS[v.metric].value(r); }
  function remax() {
    v.maxVal = Math.max(1e-9, ...rows.map(metricOf));
  }
  remax();

  /* ---- toolbar: metric picker + legend + zoom controls + export ---- */
  const metricSel = el("select", { class: "term-sel",
    title: "What the bar height shows" });
  for (const [id, m] of Object.entries(DIAG_METRICS)) {
    metricSel.append(el("option", { value: id, text: m.label }));
  }
  metricSel.value = v.metric;
  metricSel.addEventListener("change", () => {
    v.metric = metricSel.value;
    remax();
    render();
  });
  const legend = el("span", { class: "diag-legend" },
    ...Object.entries(DIAG_KINDS).map(([kind, k]) =>
      el("span", { class: "diag-key" },
        el("span", { class: "diag-swatch" }), k.label)));
  legend.querySelectorAll(".diag-swatch").forEach((sw, i) => {
    sw.style.background = diagColor(Object.keys(DIAG_KINDS)[i]);
  });
  const zbtn = (label, title, fn) => {
    const b = el("button", { class: "btn btn-sm", text: label, title });
    b.addEventListener("click", fn);
    return b;
  };
  const canvas = el("canvas", { class: "diag-canvas", tabindex: "0" });
  // pop-out / pop-in: the same view can live in the main window as a
  // tab, or in its OWN OS window (diagwin.html sets LOOM_DIAG_CHILD).
  // Child windows always close with the main window.
  const isChild = typeof window.LOOM_DIAG_CHILD === "string";
  const popBtn = isChild
    ? zbtn("⇤ Return to app",
      "Close this window and reopen the diagnostics as a tab in Loom",
      () => { Api.call("diag_popin", chatId); })
    : zbtn("⇱ Pop out",
      "Open these diagnostics in their own window (it follows the chat "
        + "live, and closes with the main window)",
      async () => {
        const res = await Api.call("diag_popout", chatId);
        if (!res.ok) { toast(res.error, "err"); return; }
        if (typeof removeTab === "function") removeTab("diag:" + chatId);
      });
  panel.append(el("div", { class: "diag-bar" },
    el("b", { text: data.title || "Chat" }),
    el("span", { class: "arc-meta", text: data.model || "" }),
    el("span", { class: "spacer" }),
    metricSel,
    legend,
    zbtn("−", "Zoom out (wheel works too)", () => zoomAt(canvas.clientWidth / 2, 1 / 1.3)),
    zbtn("+", "Zoom in", () => zoomAt(canvas.clientWidth / 2, 1.3)),
    zbtn("fit", "Fit every entry in view", () => { fitAll(); render(); }),
    zbtn("Export JSON",
      "Save a metadata/stats-only JSON snapshot - token estimates, real "
        + "usage/timings, context breakdown; NO message contents. Safe to "
        + "attach to a bug report.",
      async () => {
        const res = await Api.call("chat_diag_export", chatId);
        if (!res.ok) { toast(res.error, "err"); return; }
        if (res.data.saved) toast("Diagnostics saved to " + res.data.saved, "ok");
      }),
    popBtn));
  if (isChild) document.title = "Diagnostics · " + (data.title || "Chat");

  /* ---- the summary tiles ---- */
  panel.append(diagTiles(rows, data.nCtx));

  /* ---- the graph (fixed - never scrolls away) ---- */
  const wrap = el("div", { class: "diag-graph" }, canvas);
  panel.append(wrap);

  /* ---- the table (scrolls in its OWN region) ---- */
  const table = el("div", { class: "diag-table", tabindex: "0" });
  table.append(el("div", { class: "diag-row diag-head" },
    el("span", { text: "#" }), el("span", { text: "time" }),
    el("span", { text: "kind" }), el("span", { text: "tokens" }),
    el("span", { text: "took" }), el("span", { text: "tok/s" }),
    el("span", { text: "ctx" }),
    el("span", { text: "detail" }), el("span", { text: "preview" })));
  rows.forEach((r, idx) => {
    const re = r.kind === "assistant" ? r.real : null;
    const tps = re?.tps ? (re.tps >= 100 ? String(Math.round(re.tps))
      : re.tps.toFixed(1))
      : (r.durMs > 800 && r.tokens
         ? "~" + Math.round((r.tokens || 0) / (r.durMs / 1000)) : "");
    const tokTxt = re?.outN && r.kind === "assistant"
      ? String(re.outN) : "~" + String(r.tokens || 0);
    const rowEl = el("div", { class: "diag-row" },
      el("span", { text: String(idx + 1) }),
      el("span", { text: r.ts ? new Date(r.ts).toLocaleTimeString() : "·" }),
      el("span", {},
        (() => { const d = el("span", { class: "diag-swatch" });
                 d.style.background = diagColor(r.kind); return d; })(),
        " " + (DIAG_KINDS[r.kind]?.label || r.kind)),
      el("span", { class: "diag-num", text: tokTxt,
        title: re?.outN ? "server-reported output tokens"
          : "estimated (chars ÷ 4)" }),
      el("span", { class: "diag-num", text: fmtDur(r.durMs),
        title: re ? "prefill " + fmtDur(Math.round(re.promptMs))
          + " + generation " + fmtDur(Math.round(re.genMs))
          + (re.ttftMs ? " · ttft " + (re.ttftMs / 1000).toFixed(2) + "s" : "")
          : "" }),
      el("span", { class: "diag-num", text: tps }),
      el("span", { class: "diag-num",
        text: re?.ctxTokens ? fmtTok(re.ctxTokens) : "",
        title: re?.ctxTokens ? "context after this turn: "
          + re.ctxTokens + " tokens"
          + (re.cacheN ? " (" + fmtTok(re.cacheN) + " served from cache)" : "")
          : "" }),
      el("span", { text: r.extra || "" }),
      el("span", { class: "diag-prev", text: r.preview || "", title: r.preview || "" }));
    rowEl.addEventListener("click", () => select(idx, "table"));
    r.el = rowEl;
    table.append(rowEl);
  });
  if (!rows.length) {
    table.append(el("div", { class: "picker-empty",
      text: "Nothing here yet - send a message first." }));
  }
  panel.append(table);

  /* ---- selection: the correlation in both directions ---- */
  const PAD_L = 52, PAD_B = 18, PAD_T = 8;
  function select(idx, from, quiet) {
    if (idx < 0 || idx >= rows.length) return;
    if (v.sel >= 0) rows[v.sel].el.classList.remove("diag-sel");
    v.sel = idx;
    const r = rows[idx];
    r.el.classList.add("diag-sel");
    if (!quiet) {
      v.flashT = performance.now();
      // replay the flash animation even on re-click
      r.el.classList.remove("diag-flash");
      void r.el.offsetWidth;
      r.el.classList.add("diag-flash");
    }
    if (from === "graph") {
      r.el.scrollIntoView({ block: "center", behavior: "smooth" });
    } else {
      if (from === "keys") r.el.scrollIntoView({ block: "nearest" });
      // pan the bar into the visible window when it is not
      const plotBars = (canvas.clientWidth - PAD_L) / v.scale;
      if (idx < v.offset || idx > v.offset + plotBars - 1) {
        v.offset = Math.max(0, idx - plotBars / 2);
        clampOffset();
      }
    }
    if (!quiet) startPulse();
  }

  /* ---- rendering ---- */
  function render() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }
    const ctx2 = canvas.getContext("2d");
    ctx2.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(document.documentElement);
    ctx2.clearRect(0, 0, w, h);
    const plotW = w - PAD_L, plotH = h - PAD_B - PAD_T;
    const fmt = DIAG_METRICS[v.metric].fmt;
    // y gridlines: 4 steps of the metric scale
    ctx2.font = "10px " + css.getPropertyValue("--font-mono");
    ctx2.textAlign = "right";
    for (let g = 0; g <= 4; g++) {
      const val = v.maxVal * g / 4;
      const y = PAD_T + plotH - plotH * g / 4;
      ctx2.strokeStyle = css.getPropertyValue("--bd-0").trim() || "#333";
      ctx2.globalAlpha = g === 0 ? 1 : 0.5;
      ctx2.beginPath();
      ctx2.moveTo(PAD_L, y);
      ctx2.lineTo(w, y);
      ctx2.stroke();
      ctx2.globalAlpha = 1;
      ctx2.fillStyle = css.getPropertyValue("--fg-2").trim() || "#888";
      ctx2.fillText(g === 0 ? "0" : fmt(val), PAD_L - 6, y + 3);
    }
    // the context-window ceiling, when the Context metric is showing
    if (v.metric === "ctx" && data.nCtx && data.nCtx <= v.maxVal * 1.5) {
      const y = PAD_T + plotH - Math.min(1, data.nCtx / v.maxVal) * plotH;
      ctx2.strokeStyle = css.getPropertyValue("--err").trim() || "#f66";
      ctx2.setLineDash([4, 4]);
      ctx2.beginPath();
      ctx2.moveTo(PAD_L, y);
      ctx2.lineTo(w, y);
      ctx2.stroke();
      ctx2.setLineDash([]);
    }
    // bars
    const first = Math.max(0, Math.floor(v.offset));
    const last = Math.min(rows.length - 1,
      Math.ceil(v.offset + plotW / v.scale));
    const now = performance.now();
    for (let i = first; i <= last; i++) {
      const r = rows[i];
      const x = PAD_L + (i - v.offset) * v.scale;
      const bw = Math.max(1, v.scale * 0.72);
      const bh = Math.max(2, metricOf(r) / v.maxVal * plotH);
      ctx2.fillStyle = diagColor(r.kind);
      ctx2.globalAlpha = v.sel >= 0 && v.sel !== i ? 0.55 : 1;
      ctx2.fillRect(x, PAD_T + plotH - bh, bw, bh);
      ctx2.globalAlpha = 1;
      if (i === v.sel) {
        // the correlation pulse: an expanding, fading ring on the bar
        const age = now - v.flashT;
        const acc = css.getPropertyValue("--acc").trim() || "#fff";
        ctx2.strokeStyle = acc;
        ctx2.lineWidth = 1.5;
        ctx2.strokeRect(x - 1.5, PAD_T + plotH - bh - 1.5, bw + 3, bh + 3);
        if (age < 700) {
          const t = age / 700;
          ctx2.globalAlpha = 1 - t;
          ctx2.lineWidth = 2;
          const grow = 3 + t * 14;
          ctx2.strokeRect(x - grow, PAD_T + plotH - bh - grow,
                          bw + grow * 2, bh + grow * 2);
          ctx2.globalAlpha = 1;
        }
      }
    }
    // x captions: sparse entry numbers
    ctx2.textAlign = "center";
    ctx2.fillStyle = css.getPropertyValue("--fg-2").trim() || "#888";
    const step = Math.max(1, Math.round(60 / v.scale));
    for (let i = first - (first % step); i <= last; i += step) {
      if (i < 0) continue;
      const x = PAD_L + (i - v.offset) * v.scale + v.scale * 0.36;
      ctx2.fillText(String(i + 1), x, h - 5);
    }
  }
  v.render = render;
  v.resize = () => { clampOffset(); render(); };

  let pulseRAF = null;
  function startPulse() {
    cancelAnimationFrame(pulseRAF);
    const tick = () => {
      render();
      if (performance.now() - v.flashT < 750) {
        pulseRAF = requestAnimationFrame(tick);
      }
    };
    tick();
  }

  function clampOffset() {
    const plotBars = (canvas.clientWidth - PAD_L) / v.scale;
    v.offset = Math.max(0, Math.min(v.offset, rows.length - plotBars));
    if (rows.length <= plotBars) v.offset = 0;
  }
  function fitAll() {
    const plotW = Math.max(50, canvas.clientWidth - PAD_L);
    v.scale = Math.max(1, Math.min(40, plotW / Math.max(1, rows.length)));
    v.offset = 0;
  }
  function zoomAt(px, factor) {
    // keep the bar under the cursor stationary through the zoom
    const anchor = v.offset + Math.max(0, px - PAD_L) / v.scale;
    v.scale = Math.max(1, Math.min(80, v.scale * factor));
    v.offset = anchor - Math.max(0, px - PAD_L) / v.scale;
    clampOffset();
    render();
  }

  /* ---- interactions ---- */
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomAt(e.offsetX, e.deltaY < 0 ? 1.2 : 1 / 1.2);
  }, { passive: false });
  let drag = null;
  canvas.addEventListener("mousedown", (e) => {
    drag = { x: e.clientX, off: v.offset, moved: false };
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 3) drag.moved = true;
    v.offset = drag.off - dx / v.scale;
    clampOffset();
    render();
  }, { signal: ac.signal });
  window.addEventListener("mouseup", (e) => {
    if (!drag) return;
    const wasDrag = drag.moved;
    drag = null;
    if (wasDrag || e.target !== canvas) return;
    const rect = canvas.getBoundingClientRect();
    const idx = Math.floor(v.offset + (e.clientX - rect.left - PAD_L) / v.scale);
    if (idx >= 0 && idx < rows.length
        && e.clientY >= rect.top && e.clientY <= rect.bottom) {
      select(idx, "graph");
    }
  }, { signal: ac.signal });
  // hover: a tooltip naming the bar under the cursor (title attr = cheap)
  canvas.addEventListener("mousemove", (e) => {
    const rect = canvas.getBoundingClientRect();
    const idx = Math.floor(v.offset + (e.clientX - rect.left - PAD_L) / v.scale);
    const r = rows[idx];
    canvas.title = r
      ? "#" + (idx + 1) + " " + (DIAG_KINDS[r.kind]?.label || r.kind)
        + " · " + DIAG_METRICS[v.metric].fmt(metricOf(r))
        + (r.preview ? " - " + r.preview.slice(0, 80) : "")
      : "";
  });
  /* keyboard, tab-wide: ↑/↓ walk the SELECTION through the entries
   * (both views follow); ←/→ pan the graph, +/− zoom, Home/End jump. */
  const onKeys = (e) => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const d = e.key === "ArrowDown" ? 1 : -1;
      select(v.sel < 0 ? (d > 0 ? 0 : rows.length - 1) : v.sel + d, "keys");
      return;
    }
    const plotBars = (canvas.clientWidth - PAD_L) / v.scale;
    if (e.key === "ArrowLeft") { v.offset -= plotBars / 4; }
    else if (e.key === "ArrowRight") { v.offset += plotBars / 4; }
    else if (e.key === "+" || e.key === "=") { zoomAt(canvas.clientWidth / 2, 1.3); return e.preventDefault(); }
    else if (e.key === "-") { zoomAt(canvas.clientWidth / 2, 1 / 1.3); return e.preventDefault(); }
    else if (e.key === "Home") { v.offset = 0; if (rows.length) select(0, "keys"); }
    else if (e.key === "End") { v.offset = rows.length; if (rows.length) select(rows.length - 1, "keys"); }
    else return;
    e.preventDefault();
    clampOffset();
    render();
  };
  canvas.addEventListener("keydown", onKeys);
  table.addEventListener("keydown", onKeys);
  window.addEventListener("resize", () => v.resize(), { signal: ac.signal });

  setTimeout(() => {
    if (!prev) fitAll();   // a refresh keeps the user's zoom/pan
    clampOffset();
    render();
    if (!rows.length) return;
    if (!prev) {
      // first open: land the selection (and the eye) on the newest entry
      select(rows.length - 1, "keys");
      table.focus({ preventScroll: true });
      return;
    }
    // live refresh: follow the tail, or hold the user's exact spot -
    // and never flash, steal focus, or yank the scroll position. The
    // selection may follow the tail while the SCROLL stays put: only a
    // reader already at the bottom gets carried to the new bottom.
    if (follow) {
      select(rows.length - 1, wasAtBottom ? "keys" : "table", true);
      if (!wasAtBottom) table.scrollTop = oldTableScroll;
    } else {
      select(Math.min(prev.sel, rows.length - 1), "table", true);
      table.scrollTop = oldTableScroll;
    }
    if (hadFocus && !panel.contains(document.activeElement)) {
      table.focus({ preventScroll: true });
    }
  }, 0);
}
