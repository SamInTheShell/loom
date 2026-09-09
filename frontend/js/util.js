/* util.js - DOM helpers, icons, toasts, context menus, modals.
 * No frameworks, no modules (file:// origin). */
"use strict";

function $(sel, root) { return (root || document).querySelector(sel); }

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;           // trusted markup only
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

/* ---------- icons (inline SVG, 24-grid, stroke) ---------- */
const ICONS = {
  chat: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8z"/>',
  library: '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
  servers: '<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>',
  archive: '<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/>',
  swap: '<path d="M3 7h13l-3.5-3.5M21 17H8l3.5 3.5"/>',
  chevleft: '<path d="M15 18l-6-6 6-6"/>',
  chevright: '<path d="M9 18l6-6-6-6"/>',
  minus: '<path d="M5 12h14"/>',
  maxsq: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  restore: '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V7a2 2 0 0 1 2-2h8"/>',
  closex: '<path d="M18 6L6 18M6 6l12 12"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  yaml: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13l2 2 4-4"/>',
  md: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 17v-4l2 2 2-2v4"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  terminal: '<path d="M4 17l6-5-6-5"/><path d="M12 19h8"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  fork: '<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/>',
  up: '<path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>',
  down: '<path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  refresh: '<path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.9-3L23 10M1 14l4.6 4a9 9 0 0 0 14.9-3"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  brain: '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M12 5v13"/>',
  box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.29 7 12 12l8.71-5"/><path d="M12 22V12"/>',
  pin: '<path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z"/>',
  key: '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
  chart: '<path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6"/><rect x="12" y="7" width="3" height="11"/><rect x="17" y="10" width="3" height="8"/>',
  // the MCP logo glyph (from frontend/3rdparty/mcp-dark-icon.svg), scaled
  // from its 195-unit box into ours; stroke-width 16 ≈ 2 after the scale
  mcp: '<g transform="scale(0.1231)" stroke-width="16">'
    + '<path d="M25 97.8528L92.8822 29.9706C102.255 20.598 117.451 20.598 126.823 29.9706V29.9706C136.196 39.3431 136.196 54.5391 126.823 63.9117L75.5581 115.177"/>'
    + '<path d="M76.2652 114.47L126.823 63.9117C136.196 54.5391 151.392 54.5391 160.765 63.9117L161.118 64.2652C170.491 73.6378 170.491 88.8338 161.118 98.2063L99.7248 159.6C96.6006 162.724 96.6006 167.789 99.7248 170.913L112.331 183.52"/>'
    + '<path d="M109.853 46.9411L59.6482 97.1457C50.2756 106.518 50.2756 121.714 59.6482 131.087V131.087C69.0208 140.459 84.2167 140.459 93.5893 131.087L143.794 80.8822"/></g>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
};

function icon(name, size) {
  const s = size || 16;
  return `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.file}</svg>`;
}

function iconFor(fileName) {
  const n = String(fileName || "").toLowerCase();
  if (n.endsWith(".md") || n.endsWith(".markdown")) return "md";
  if (n.endsWith(".yaml") || n.endsWith(".yml")) return "yaml";
  return "file";
}

/* ---------- toasts + the notification log ----------
 * Every toast is also recorded in NotifLog, so a message that auto-
 * dismissed is never lost - the bell in the top bar opens the history.
 * Toasts are selectable, hover pauses the auto-dismiss, × dismisses. */
const NotifLog = { items: [], unseen: 0 };

function notifRecord(msg, level) {
  const item = { ts: Date.now(), level: level || "info", msg: String(msg) };
  NotifLog.items.push(item);
  if (NotifLog.items.length > 500) NotifLog.items.shift();
  NotifLog.unseen++;
  if (typeof renderNotifBadge === "function") renderNotifBadge();
  // alerts persist per library (the Alerts tab) until the user clears them
  if (typeof st !== "undefined" && st.library
      && typeof Api !== "undefined" && Api.real()) {
    Api.call("alert_add", item).then(() => {
      if (st.activeTab === "alerts") refreshAlertsTab();
    });
  }
}

function toast(msg, level, ms) {
  notifRecord(msg, level);
  const root = $("#toast-root");
  const t = el("div", { class: "toast " + (level || "") },
    el("div", { class: "toast-msg", text: String(msg) }),
    el("button", {
      class: "toast-x", text: "×", title: "Dismiss",
      onclick: () => t.remove(),
    }));
  root.append(t);
  let timer = setTimeout(() => t.remove(), ms || (level === "err" ? 12000 : 5000));
  t.addEventListener("mouseenter", () => clearTimeout(timer));
  t.addEventListener("mouseleave", () => {
    timer = setTimeout(() => t.remove(), 4000);
  });
}

/* ---------- clipboard ---------- */
function copyText(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (ok) return Promise.resolve(true);
  } catch (e) { /* fall through */ }
  if (navigator.clipboard) return navigator.clipboard.writeText(text).then(() => true, () => false);
  return Promise.resolve(false);
}

/* ---------- context menus / overlay bookkeeping ----------
 * ONE overlay (#ctx-root) hosts every context menu and popup. Its
 * outside-click listeners MUST be torn down on close: #ctx-root persists,
 * so a listener left behind still points at its old, detached menu -
 * menu.contains() is then false for everything, and any click (even
 * inside the next menu) instantly closes it. _overlayCleanup owns the
 * teardown; closeCtx() runs it. */
let _overlayCleanup = null;
let _overlayClose = null;   // the polite close (runs the menu's onclose hook)

function closeCtx() {
  _overlayClose = null;
  if (_overlayCleanup) {
    const f = _overlayCleanup;
    _overlayCleanup = null;
    try { f(); } catch (e) { /* teardown is best-effort */ }
  }
  $("#ctx-root").replaceChildren();
}

/* close the open menu the way its builder intended (Escape, hotkeys) */
function closeCtxTop() {
  const f = _overlayClose;
  _overlayClose = null;
  if (f) f(); else closeCtx();
}

/* ---------- menu keyboard: arrows walk, Enter picks, 1-9 jump ---------- */
const MENU_ITEM_SEL = ".ctx-item, .popup-item";

/* MENU_ITEM_SEL with a class suffix on EVERY alternative. Naive string
 * concatenation binds the suffix to the last alternative only -
 * ".ctx-item, .popup-item.sel" matches every bare .ctx-item, which
 * broke arrow navigation in every .ctx-item menu (the cursor never
 * seeded and always "started" at the first row). */
function _menuSel(suffix) {
  return MENU_ITEM_SEL.split(",")
    .map((s) => s.trim() + suffix).join(", ");
}

function _menuItems(menu) {
  return [...menu.querySelectorAll(MENU_ITEM_SEL)];
}

/* every menu item gets a faint number - its hotkey while the menu is open.
 * Idempotent: the MutationObserver that calls it must settle. */
function _numberMenuItems(menu) {
  _menuItems(menu).forEach((it, i) => {
    let num = it.querySelector(":scope > .menu-num");
    if (i < 9) {
      const label = String(i + 1);
      if (!num) it.prepend(el("span", { class: "menu-num", text: label }));
      else if (num.textContent !== label) num.textContent = label;
    } else if (num) {
      num.remove();
    }
  });
}

function _menuKeyHandler(menu, close) {
  const selected = () => menu.querySelector(_menuSel(".kbd-sel"));
  const setSel = (item) => {
    selected()?.classList.remove("kbd-sel");
    if (item) {
      item.classList.add("kbd-sel");
      item.scrollIntoView({ block: "nearest" });
    }
  };
  menu._kbdSetSel = setSel;   // menus re-seed the cursor after re-renders
  const move = (delta) => {
    const items = _menuItems(menu);
    if (!items.length) return;
    // no cursor yet: anchor on the menu's own selection (the active
    // model/mode row carries .sel) so arrows continue from it
    let i = items.indexOf(selected());
    if (i < 0) {
      const anchor = items.indexOf(menu.querySelector(_menuSel(".sel")));
      if (anchor >= 0) i = anchor;
      else {
        setSel(items[delta > 0 ? 0 : items.length - 1]);
        return;
      }
    }
    setSel(items[(i + delta + items.length) % items.length]);
  };
  return (e) => {
    if (e.altKey || e.metaKey) return;
    const inField = e.target.closest
      && e.target.closest("input, textarea, [contenteditable='true']");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      // vertical arrows belong to a focused VALUE control (selects
      // cycle options, ranges/numbers step) - text fields keep menu
      // navigation (the model menu's filter drives its list)
      if (e.target.closest && e.target.closest(
          "select, input[type='range'], input[type='number']")) {
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      move(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "ArrowRight" && !inField) {
      // descend into a submenu: rows whose CLICK descends carry
      // data-submenu; rows whose click picks a value but still have a
      // deeper layer (a model's reasoning) register row._submenu instead
      const cur = selected() || menu.querySelector(_menuSel(".sel"));
      if (cur && typeof cur._submenu === "function") {
        e.preventDefault();
        e.stopPropagation();
        cur._submenu();
      } else if (cur && cur.dataset.submenu !== undefined) {
        e.preventDefault();
        e.stopPropagation();
        cur.click();
      }
    } else if (e.key === "ArrowLeft" && !inField) {
      // ascend: the menu registers its back action as menu._back
      if (typeof menu._back === "function") {
        e.preventDefault();
        e.stopPropagation();
        menu._back();
      }
    } else if ((e.key === "Home" || e.key === "End") && !inField) {
      e.preventDefault();
      e.stopPropagation();
      const items = _menuItems(menu);
      setSel(items[e.key === "Home" ? 0 : items.length - 1]);
    } else if (e.key === "Enter") {
      const cur = selected();
      if (cur) {
        e.preventDefault();
        e.stopPropagation();
        cur.click();
      }
    } else if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !inField) {
      const it = _menuItems(menu)[Number(e.key) - 1];
      if (it) {
        e.preventDefault();
        e.stopPropagation();
        it.click();
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };
}

function _armOverlay(menu, close) {
  const root = $("#ctx-root");
  const onDown = (e) => { if (!menu.contains(e.target)) close(); };
  const onCtxEv = (e) => { e.preventDefault(); close(); };
  root.addEventListener("mousedown", onDown);
  root.addEventListener("contextmenu", onCtxEv);
  const onKeys = _menuKeyHandler(menu, close);
  document.addEventListener("keydown", onKeys, true);
  // the ACTIVE row is where Up/Down start from - seed the keyboard cursor
  // on it the moment it exists, and RE-seed after any async re-render
  // that wiped it (lists that fill in later, live state redraws). This is
  // what makes the first arrow press move relative to the current value
  // instead of jumping to the top of the list.
  const seedCursor = () => {
    if (menu.querySelector(_menuSel(".kbd-sel"))) return;
    // the current selection is the starting position; a menu with no
    // selection reference starts at the top
    const anchor = menu.querySelector(_menuSel(".sel")) || _menuItems(menu)[0];
    anchor?.classList.add("kbd-sel");
  };
  // popup menus fill their lists asynchronously - number whatever appears
  const obs = new MutationObserver(() => { _numberMenuItems(menu); seedCursor(); });
  obs.observe(menu, { childList: true, subtree: true });
  _numberMenuItems(menu);
  seedCursor();
  _overlayClose = close;
  _overlayCleanup = () => {
    root.removeEventListener("mousedown", onDown);
    root.removeEventListener("contextmenu", onCtxEv);
    document.removeEventListener("keydown", onKeys, true);
    obs.disconnect();
  };
}

function ctxMenu(x, y, items) {
  closeCtx();
  const root = $("#ctx-root");
  const menu = el("div", { class: "ctxmenu" });
  for (const it of items) {
    if (it === "-") { menu.append(el("div", { class: "ctx-sep" })); continue; }
    menu.append(el("div", {
      class: "ctx-item" + (it.danger ? " danger" : ""),
      text: it.label,
      onclick: () => { closeCtx(); it.fn && it.fn(); },
    }));
  }
  root.append(menu);
  _armOverlay(menu, closeCtx);
  // clamp into the viewport
  menu.style.left = "0px"; menu.style.top = "0px";
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - r.width - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - r.height - 8) + "px";
}

/* ---------- anchored popup menus ----------
 * A rounded popup that opens ABOVE its anchor (right edges aligned) -
 * for controls near the bottom of the window, e.g. the composer's model
 * and permission-mode menus. Falls below only when there is no room.
 * build(menu, close) fills it; returns {menu, close}. */
function popupMenu(anchor, build) {
  closeCtx();   // clears any previous overlay AND its listeners
  const root = $("#ctx-root");
  const menu = el("div", { class: "popup" });
  const close = () => {
    if (menu._onclose) { try { menu._onclose(); } catch (e) { /* noop */ } }
    closeCtx();
  };
  build(menu, close);
  root.append(menu);
  const a = anchor.getBoundingClientRect();
  const r = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(a.right - r.width, window.innerWidth - r.width - 8));
  menu.style.left = left + "px";
  // anchor ABOVE the button by the BOTTOM edge: content that loads in
  // later (async lists) grows UPWARD and can never spill past the window
  // bottom. max-height caps growth to the space above; only when there is
  // almost none does the menu flip below (capped to the space there).
  const spaceAbove = a.top - 16;
  const spaceBelow = window.innerHeight - a.bottom - 16;
  if (spaceAbove >= 140 || spaceAbove >= spaceBelow) {
    menu.style.bottom = (window.innerHeight - a.top + 8) + "px";
    menu.style.maxHeight = Math.min(340, Math.max(100, spaceAbove)) + "px";
  } else {
    menu.style.top = (a.bottom + 8) + "px";
    menu.style.maxHeight = Math.min(340, Math.max(100, spaceBelow)) + "px";
  }
  _armOverlay(menu, close);
  return { menu, close };
}

/* ---------- modals ---------- */
function closeTopModal() {
  const root = $("#modal-root");
  if (!root.firstChild) return false;
  root.lastChild.remove();
  return true;
}

function modal(title, bodyNodes, buttons, opts) {
  const root = $("#modal-root");
  // opts.id makes the modal a SINGLETON: a second open with the same id
  // replaces the first - repeated triggers (quit clicks, repeated events)
  // must never stack copies on screen
  if (opts && opts.id) {
    for (const old of root.querySelectorAll(
      `.modal[data-modal-id="${CSS.escape(opts.id)}"]`)) {
      old.remove();
    }
  }
  const m = el("div", { class: "modal" },
    el("div", { class: "modal-head", text: title }),
    el("div", { class: "modal-body" }, ...bodyNodes),
    el("div", { class: "modal-foot" },
      ...buttons.map((b) => el("button", {
        class: "btn " + (b.cls || ""), text: b.label,
        onclick: () => { if (!b.fn || b.fn() !== false) m.remove(); },
      }))));
  if (opts && opts.id) m.dataset.modalId = opts.id;
  root.append(m);
  _wireModalKeys(m);
  return m;
}

/* ---------- dialog keyboard ----------
 * Esc closes (global handler); Enter fires the primary action - the last
 * enabled .btn-acc/.btn-danger anywhere in the dialog, so multi-step
 * bodies (the model wizard) get their Next/Add button too. Ctrl+Enter and
 * Ctrl+Esc work identically ("same deal" as permission cards). Tab is
 * trapped inside the dialog, and focus lands in it on open. */
function _modalPrimary(m) {
  return [...m.querySelectorAll(".btn-acc, .btn-danger")]
    .filter((b) => !b.disabled && b.getBoundingClientRect().width)
    .pop() || null;
}

function _wireModalKeys(m) {
  // dialogs BORROW focus - closing must hand it back to whatever had it
  // (the composer keeps its caret through a "start the model?" detour)
  const prevFocus = document.activeElement;
  const origRemove = m.remove.bind(m);
  m.remove = () => {
    origRemove();
    if (prevFocus?.isConnected
        && (document.activeElement === document.body
            || document.activeElement === null)) {
      prevFocus.focus?.();
    }
  };
  const foot = m.querySelector(".modal-foot");
  const dismiss = [...foot.querySelectorAll("button")]
    .find((b) => /^(cancel|close)$/i.test(b.textContent.trim()));
  if (dismiss) setHotkey(dismiss, "Esc");
  const primary0 = _modalPrimary(m);
  if (primary0 && primary0 !== dismiss) setHotkey(primary0, "Enter");
  m.tabIndex = -1;
  m.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.metaKey) {
      if (e.defaultPrevented) return;           // a field already consumed it
      if (e.target.closest("button")) return;   // the focused button takes it
      // plain Enter keeps typing in multiline fields; Ctrl+Enter always fires
      if (!e.ctrlKey && e.target.closest("textarea, .md-surface, select")) return;
      const primary = _modalPrimary(m);
      if (primary) {
        e.preventDefault();
        e.stopPropagation();
        primary.click();
      }
    } else if (e.key === "Tab") {
      const f = [...m.querySelectorAll(
        "button, input, select, textarea, [tabindex='0'], [contenteditable='true']")]
        .filter((n) => !n.disabled && n.getBoundingClientRect().width);
      if (!f.length) return;
      let j = f.indexOf(document.activeElement) + (e.shiftKey ? -1 : 1);
      if (j >= f.length || f.indexOf(document.activeElement) < 0) j = 0;
      if (j < 0) j = f.length - 1;
      e.preventDefault();
      f[j].focus();
    }
  });
  setTimeout(() => {
    if (m.contains(document.activeElement)) return;   // builder focused already
    const first = m.querySelector(
      ".modal-body input:not([type='checkbox']):not([type='radio']), "
      + ".modal-body select, .modal-body textarea");
    (first || _modalPrimary(m) || m).focus();
  }, 0);
}

function confirmModal(title, text, okLabel, fn, danger, id) {
  // id makes the confirm a SINGLETON - repeated triggers (Enter spam on a
  // gated send, repeated events) replace the dialog instead of stacking
  modal(title, [el("p", { text })], [
    { label: "Cancel" },
    { label: okLabel || "OK", cls: danger ? "btn-danger" : "btn-acc", fn },
  ], id ? { id } : undefined);
}

function promptModal(title, text, initial, fn, okLabel) {
  const input = el("input", { type: "text", value: initial || "" });
  const m = modal(title, [text ? el("p", { text }) : null, input].filter(Boolean), [
    { label: "Cancel" },
    { label: okLabel || "OK", cls: "btn-acc", fn: () => fn(input.value) },
  ]);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); fn(input.value); m.remove(); }
  });
  setTimeout(() => { input.focus(); input.select(); }, 0);
}

/* ---------- links ----------
 * The rule: a target with a REAL URI scheme (https:, mailto:, …) belongs
 * to the OS - but only after the user confirms in-app; anything else is
 * a library document. The webview itself never navigates. */
function isAbsoluteUri(s) {
  return /^[a-z][a-z0-9+.-]*:/i.test(String(s || ""));
}

function confirmOpenExternal(url) {
  modal("Open external link?",
    [el("p", { text: "Hand this link to your system's default app?" }),
     el("p", { class: "ext-url", text: String(url) })],
    [
      { label: "Cancel" },
      {
        label: "Open", cls: "btn-acc",
        fn: async () => {
          const r = await Api.call("open_external", String(url));
          if (!r.ok) toast(r.error, "err");
        },
      },
    ], { id: "open-ext" });
}

/* ---------- misc ---------- */
function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

/* a live relative timestamp: the element carries its epoch and a global
 * ticker (main.js) refreshes every one on screen - "just now" must not
 * stay "just now" while the window sits open */
function tago(ts) {
  return el("span", { "data-tago": String(ts || 0), text: timeAgo(ts) });
}

function baseName(p) {
  const parts = String(p || "").replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || p;
}

function fmtTok(n) {
  n = Number(n);
  if (!Number.isFinite(n) || n < 0) n = 0;
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(Math.round(n));
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(2) + " GB";
}

function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* markdown → sanitized HTML (marked + DOMPurify, both vendored) */
function renderMarkdown(text) {
  try {
    const html = marked.parse(String(text ?? ""), { breaks: true, gfm: true });
    return DOMPurify.sanitize(html);
  } catch (e) {
    return esc(text);
  }
}

/* Full-panel re-renders (replaceChildren) destroy the focused input -
 * mid-typing, a background event would silently steal the caret. Inputs
 * that must survive carry data-keep="<key>"; capture before the rebuild,
 * restore after. */
function captureFocus(panel) {
  const a = document.activeElement;
  if (!a || !panel || !panel.contains(a) || !a.dataset || !a.dataset.keep) return null;
  return { keep: a.dataset.keep, s: a.selectionStart, e: a.selectionEnd };
}

function restoreFocus(panel, f) {
  if (!f || !panel) return;
  const n = panel.querySelector(`[data-keep="${CSS.escape(f.keep)}"]`);
  if (!n) return;
  n.focus();
  try { n.setSelectionRange(f.s, f.e); } catch (e) { /* not a text input */ }
}
