/* diag.js — context usage diagnostics for one chat: a per-entry bar
 * graph over a spreadsheet-style table of the same entries.
 *
 * Graph: x is chronological order (left → right, uniform bar width), y
 * is the METRIC picked in the dropdown — Tokens (default), Tok/s, or
 * Duration. Bars are color-coded by kind (user / assistant / thoughts /
 * tools / compaction). Drag pans, the wheel zooms around the cursor
 * (buttons too), clicking a bar jumps the table to that entry — and the
 * selection pulses in BOTH places so the correlation is unmissable.
 * Table rows click back. Opened from the context chip's hover card. */
"use strict";

const DIAG_KINDS = {
  user: { label: "user", color: "--run" },
  assistant: { label: "model", color: "--ok" },
  think: { label: "thoughts", color: null, fallback: "#a78bfa" },
  tool: { label: "tools", color: "--warn" },
  compact: { label: "compaction", color: "--fg-2" },
};

const DIAG_METRICS = {
  tokens: { label: "Tokens", value: (r) => r.tokens || 0,
            fmt: (x) => fmtTok(x) },
  tps: { label: "Tok/s",
         value: (r) => (r.durMs > 0 ? (r.tokens || 0) / (r.durMs / 1000) : 0),
         fmt: (x) => (x >= 100 ? Math.round(x) : x.toFixed(1)) + "/s" },
  dur: { label: "Duration", value: (r) => r.durMs || 0,
         fmt: (x) => fmtDur(Math.round(x)) },
};

function diagTabTitle(chatId) {
  const t = st.chats[chatId]?.chat?.title;
  return "Diag — " + (t || "chat");
}

function openDiagTab(chatId) {
  openTab("diag", chatId);
}

function fmtDur(ms) {
  ms = Number(ms) || 0;
  if (ms <= 0) return "—";
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

function diagColor(kind) {
  const k = DIAG_KINDS[kind] || DIAG_KINDS.tool;
  if (k.color) {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(k.color).trim();
    if (v) return v;
  }
  return k.fallback || "#888";
}

function buildDiagView(panel, chatId, data) {
  panel.replaceChildren();
  const rows = data.rows || [];

  const v = {
    rows,
    metric: "tokens",  // tokens | tps | dur — the dropdown drives it
    maxVal: 1,
    scale: 10,        // px per bar
    offset: 0,        // first visible bar (float, in bar units)
    sel: -1,
    flashT: 0,        // performance.now() of the last selection (pulse)
  };
  st._diagViews = st._diagViews || {};
  st._diagViews[chatId] = v;

  function metricOf(r) { return DIAG_METRICS[v.metric].value(r); }
  function remax() {
    v.maxVal = Math.max(1e-9, ...rows.map(metricOf));
  }
  remax();

  /* ---- toolbar: metric picker + legend + zoom controls ---- */
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
  const totalTok = rows.reduce((a, r) => a + (r.tokens || 0), 0);
  const totalDur = rows.reduce((a, r) => a + (r.durMs || 0), 0);
  const canvas = el("canvas", { class: "diag-canvas", tabindex: "0" });
  panel.append(el("div", { class: "diag-bar" },
    el("b", { text: data.title || "Chat" }),
    el("span", { class: "arc-meta",
      text: rows.length + " entries · ~" + fmtTok(totalTok) + " tok · "
        + fmtDur(totalDur) + " active"
        + (data.nCtx ? " · window " + fmtTok(data.nCtx) : "") }),
    el("span", { class: "spacer" }),
    metricSel,
    legend,
    zbtn("−", "Zoom out (wheel works too)", () => zoomAt(canvas.clientWidth / 2, 1 / 1.3)),
    zbtn("+", "Zoom in", () => zoomAt(canvas.clientWidth / 2, 1.3)),
    zbtn("fit", "Fit every entry in view", () => { fitAll(); render(); }),
    zbtn("Export JSON",
      "Save a metadata/stats-only JSON snapshot — token estimates, real "
        + "usage/timings, context breakdown; NO message contents. Safe to "
        + "attach to a bug report.",
      async () => {
        const res = await Api.call("chat_diag_export", chatId);
        if (!res.ok) { toast(res.error, "err"); return; }
        if (res.data.saved) toast("Diagnostics saved to " + res.data.saved, "ok");
      })));

  /* ---- the graph ---- */
  const wrap = el("div", { class: "diag-graph" }, canvas);
  panel.append(wrap);

  /* ---- the table ---- */
  const table = el("div", { class: "diag-table" });
  table.append(el("div", { class: "diag-row diag-head" },
    el("span", { text: "#" }), el("span", { text: "time" }),
    el("span", { text: "kind" }), el("span", { text: "tokens" }),
    el("span", { text: "took" }),
    el("span", { text: "detail" }), el("span", { text: "preview" })));
  rows.forEach((r, idx) => {
    const rowEl = el("div", { class: "diag-row" },
      el("span", { text: String(idx + 1) }),
      el("span", { text: r.ts ? new Date(r.ts).toLocaleTimeString() : "—" }),
      el("span", {},
        (() => { const d = el("span", { class: "diag-swatch" });
                 d.style.background = diagColor(r.kind); return d; })(),
        " " + (DIAG_KINDS[r.kind]?.label || r.kind)),
      el("span", { class: "diag-num", text: String(r.tokens || 0) }),
      el("span", { class: "diag-num", text: fmtDur(r.durMs) }),
      el("span", { text: r.extra || "" }),
      el("span", { class: "diag-prev", text: r.preview || "", title: r.preview || "" }));
    rowEl.addEventListener("click", () => select(idx, "table"));
    r.el = rowEl;
    table.append(rowEl);
  });
  panel.append(table);

  /* ---- selection: the correlation in both directions ---- */
  function select(idx, from) {
    if (idx < 0 || idx >= rows.length) return;
    if (v.sel >= 0) rows[v.sel].el.classList.remove("diag-sel");
    v.sel = idx;
    v.flashT = performance.now();
    const r = rows[idx];
    r.el.classList.add("diag-sel");
    // replay the flash animation even on re-click
    r.el.classList.remove("diag-flash");
    void r.el.offsetWidth;
    r.el.classList.add("diag-flash");
    if (from === "graph") {
      r.el.scrollIntoView({ block: "center", behavior: "smooth" });
    } else {
      // pan the bar into the visible window when it is not
      const plotBars = (canvas.clientWidth - PAD_L) / v.scale;
      if (idx < v.offset || idx > v.offset + plotBars - 1) {
        v.offset = Math.max(0, idx - plotBars / 2);
        clampOffset();
      }
    }
    startPulse();
  }

  /* ---- rendering ---- */
  const PAD_L = 52, PAD_B = 18, PAD_T = 8;
  function render() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(document.documentElement);
    ctx.clearRect(0, 0, w, h);
    const plotW = w - PAD_L, plotH = h - PAD_B - PAD_T;
    const fmt = DIAG_METRICS[v.metric].fmt;
    // y gridlines: 4 steps of the metric scale
    ctx.font = "10px " + css.getPropertyValue("--font-mono");
    ctx.textAlign = "right";
    for (let g = 0; g <= 4; g++) {
      const val = v.maxVal * g / 4;
      const y = PAD_T + plotH - plotH * g / 4;
      ctx.strokeStyle = css.getPropertyValue("--bd-0").trim() || "#333";
      ctx.globalAlpha = g === 0 ? 1 : 0.5;
      ctx.beginPath();
      ctx.moveTo(PAD_L, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = css.getPropertyValue("--fg-2").trim() || "#888";
      ctx.fillText(g === 0 ? "0" : fmt(val), PAD_L - 6, y + 3);
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
      ctx.fillStyle = diagColor(r.kind);
      ctx.globalAlpha = v.sel >= 0 && v.sel !== i ? 0.55 : 1;
      ctx.fillRect(x, PAD_T + plotH - bh, bw, bh);
      ctx.globalAlpha = 1;
      if (i === v.sel) {
        // the correlation pulse: an expanding, fading ring on the bar
        const age = now - v.flashT;
        const acc = css.getPropertyValue("--acc").trim() || "#fff";
        ctx.strokeStyle = acc;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x - 1.5, PAD_T + plotH - bh - 1.5, bw + 3, bh + 3);
        if (age < 700) {
          const t = age / 700;
          ctx.globalAlpha = 1 - t;
          ctx.lineWidth = 2;
          const grow = 3 + t * 14;
          ctx.strokeRect(x - grow, PAD_T + plotH - bh - grow,
                         bw + grow * 2, bh + grow * 2);
          ctx.globalAlpha = 1;
        }
      }
    }
    // x captions: sparse entry numbers
    ctx.textAlign = "center";
    ctx.fillStyle = css.getPropertyValue("--fg-2").trim() || "#888";
    const step = Math.max(1, Math.round(60 / v.scale));
    for (let i = first - (first % step); i <= last; i += step) {
      if (i < 0) continue;
      const x = PAD_L + (i - v.offset) * v.scale + v.scale * 0.36;
      ctx.fillText(String(i + 1), x, h - 5);
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
  });
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
  });
  // keyboard: arrows pan, +/- zoom, Home/End jump
  canvas.addEventListener("keydown", (e) => {
    const plotBars = (canvas.clientWidth - PAD_L) / v.scale;
    if (e.key === "ArrowLeft") { v.offset -= plotBars / 4; }
    else if (e.key === "ArrowRight") { v.offset += plotBars / 4; }
    else if (e.key === "+" || e.key === "=") { zoomAt(canvas.clientWidth / 2, 1.3); return e.preventDefault(); }
    else if (e.key === "-") { zoomAt(canvas.clientWidth / 2, 1 / 1.3); return e.preventDefault(); }
    else if (e.key === "Home") { v.offset = 0; }
    else if (e.key === "End") { v.offset = rows.length; }
    else return;
    e.preventDefault();
    clampOffset();
    render();
  });
  window.addEventListener("resize", () => v.resize());

  setTimeout(() => { fitAll(); render(); }, 0);
}
