/* tabs.js — the tab container that fills the app below the top bar.
 *
 * Tab ids: the singleton tabs are 'library' | 'servers' | 'archive' (one
 * instance each — opening again just activates); chat tabs are
 * 'chat:<chatId>'. Every tab has an icon for its type and a close button;
 * closing is gated per type (dirty editor → save/discard, streaming chat
 * → confirm cancellation, then archive).
 */
"use strict";

const TAB_META = {
  library: { icon: "library", title: () => "Library" },
  servers: { icon: "servers", title: () => "Servers" },
  archive: { icon: "archive", title: () => "Chat Archive" },
  models: { icon: "download", title: () => "Models" },
  alerts: { icon: "bell", title: () => "Alerts" },
  envs: { icon: "key", title: () => "Environments" },
  chat: {
    icon: "chat",
    title: (tab) => (st.chats[tab.chatId]?.chat?.title) || "Chat",
  },
  term: {
    icon: "terminal",
    title: (tab) => termTabTitle(tab.chatId),
  },
  diag: {
    icon: "chart",
    title: (tab) => diagTabTitle(tab.chatId),
  },
};

function tabById(id) { return st.tabs.find((t) => t.id === id); }

/* ---------- navigation history (the back/forward buttons) ----------
 * Locations are {tab, file?}: every tab visit and every document opened
 * in the Library tab. Browser semantics: going somewhere new truncates
 * the forward stack; back/forward replay without recording. */
const NAV_MAX = 100;
st.nav = { stack: [], idx: -1, applying: false };

function navCurrent() { return st.nav.stack[st.nav.idx] || null; }

function pushNav(loc) {
  if (st.nav.applying || st.restoring || !loc?.tab) return;
  const cur = navCurrent();
  if (cur && cur.tab === loc.tab && (cur.file || null) === (loc.file || null)) return;
  st.nav.stack.length = st.nav.idx + 1;   // drop the forward branch
  st.nav.stack.push({ tab: loc.tab, file: loc.file || null });
  if (st.nav.stack.length > NAV_MAX) st.nav.stack.shift();
  st.nav.idx = st.nav.stack.length - 1;
  renderNavButtons();
}

function _applyNav(loc) {
  st.nav.applying = true;
  try {
    const [type, sub] = loc.tab.includes(":")
      ? [loc.tab.split(":")[0], loc.tab.split(":").slice(1).join(":")]
      : [loc.tab, null];
    if (type === "chat") {
      openChat(sub);                       // reopens (un-archives) if needed
    } else if (type === "term" || type === "diag") {
      if (tabById(loc.tab)) activateTab(loc.tab);
      else return false;                   // dead tab — skip over it
    } else if (TAB_META[type]) {
      openTab(type);
      if (type === "library" && loc.file && loc.file !== st.lib.open) {
        setTimeout(() => openLibFile(loc.file), 30);
      }
    } else {
      return false;
    }
    return true;
  } finally {
    setTimeout(() => { st.nav.applying = false; renderNavButtons(); }, 60);
  }
}

function navGo(delta) {
  let i = st.nav.idx + delta;
  while (i >= 0 && i < st.nav.stack.length) {
    st.nav.idx = i;
    if (_applyNav(st.nav.stack[i])) return;
    i += delta;                            // location no longer exists — keep going
  }
  renderNavButtons();
}

function renderNavButtons() {
  const back = $("#btn-back");
  const fwd = $("#btn-fwd");
  if (!back || !fwd) return;
  back.disabled = st.nav.idx <= 0;
  fwd.disabled = st.nav.idx >= st.nav.stack.length - 1;
}

function resetNav() {
  st.nav.stack = [];
  st.nav.idx = -1;
  renderNavButtons();
}

function panelFor(id) {
  return $("#tabpanels").querySelector(`[data-tab="${CSS.escape(id)}"]`);
}

function openTab(type, chatId) {
  const id = (type === "chat" || type === "term" || type === "diag")
    ? type + ":" + chatId : type;
  let tab = tabById(id);
  if (!tab) {
    tab = { id, type, chatId };
    st.tabs.push(tab);
    const panel = el("div", { class: "tabpanel hidden", "data-tab": id });
    $("#tabpanels").append(panel);
    mountTab(tab, panel);
  }
  activateTab(id);
  return tab;
}

function mountTab(tab, panel) {
  if (tab.type === "library") mountLibraryTab(panel);
  else if (tab.type === "servers") mountServersTab(panel);
  else if (tab.type === "archive") mountArchiveTab(panel);
  else if (tab.type === "models") mountModelsTab(panel);
  else if (tab.type === "alerts") mountAlertsTab(panel);
  else if (tab.type === "envs") mountEnvsTab(panel);
  else if (tab.type === "chat") mountChatTab(panel, tab.chatId);
  else if (tab.type === "term") mountTermTab(panel, tab.chatId);
  else if (tab.type === "diag") mountDiagTab(panel, tab.chatId);
}

function activateTab(id) {
  st.activeTab = id;
  for (const p of $("#tabpanels").children) {
    p.classList.toggle("hidden", p.dataset.tab !== id);
  }
  renderTabs();
  saveSession();
  if (typeof closeCtxCard === "function") closeCtxCard();
  pushNav({ tab: id, file: id === "library" ? st.lib.open : null });
  const tab = tabById(id);
  if (tab?.type === "servers") refreshServersTab();
  if (tab?.type === "archive") refreshArchiveTab();
  if (tab?.type === "alerts") {
    NotifLog.unseen = 0;
    renderNotifBadge();
    refreshAlertsTab();
  }
  if (tab?.type === "chat") {
    // display:none dropped the thread's scroll — put it back. A user who
    // was AT THE BOTTOM gets the NEW bottom (the stream may have grown
    // while the tab was hidden — auto-follow must re-arm); one who had
    // scrolled up to compare gets their exact spot.
    const cs = st.chats[tab.chatId];
    if (cs && cs.restoreScroll == null && cs.scrollPos != null) {
      cs.restoreScroll = cs.atBottom === false ? cs.scrollPos : Infinity;
    }
    queueThreadRedraw(tab.chatId);
    setTimeout(() => panelFor(id)?.querySelector(".compose-input")?.focus(), 0);
  }
  if (tab?.type === "library" && st.lib.editor) {
    // same for the editor pane
    const sc = st.lib.editor.scroller;
    setTimeout(() => { sc.scrollTop = st.lib.scrollPos || 0; }, 0);
  }
  if (tab?.type === "diag") {
    // display:none canvases have zero size — re-measure on activation
    setTimeout(() => st._diagViews?.[tab.chatId]?.resize?.(), 0);
  }
  if (tab?.type === "term") {
    st._lastTermId = tab.chatId;   // newTerminal() inherits from here
    // re-engage the terminal: re-measure (it was display:none), repaint
    // whatever streamed while hidden, reattach a lost session, and focus
    // the shell so typing works immediately
    const v = st._termViews?.[tab.chatId];
    if (v && !v.dead) {
      setTimeout(() => {
        v.measure?.();
        scheduleTermPaint(v);
        if (!v.opening && !v.exited && !st.terms[tab.chatId]?.running) {
          termAttachOrOpen(tab.chatId, v);
        }
        v.el.focus();
      }, 0);
    }
  }
}

/* closeTab: the gates live with the tab types. Actual removal is
 * removeTab(); this is the polite ask. */
function closeTab(id) {
  const tab = tabById(id);
  if (!tab) return;
  if (tab.type === "library" && st.lib.dirty) {
    confirmModal("Unsaved changes",
      "The open file has unsaved changes. Discard them and close the tab?",
      "Discard & close", () => { st.lib.dirty = false; removeTab(id); }, true);
    return;
  }
  if (tab.type === "chat") {
    closeChatTab(tab.chatId);   // chat.js owns the archive/cancel gate
    return;
  }
  if (tab.type === "term") {
    closeTerminalTab(tab.chatId);   // terminal.js owns the busy guard
    return;
  }
  removeTab(id);
}

function removeTab(id) {
  const i = st.tabs.findIndex((t) => t.id === id);
  if (i < 0) return;
  const tab = st.tabs[i];
  if (tab.type === "library" && st.lib.editor) {
    st.lib.editor.destroy();
    st.lib.editor = null;
    st.lib.open = null;
    st.lib.dirty = false;
  }
  st.tabs.splice(i, 1);
  panelFor(id)?.remove();
  if (st.activeTab === id) {
    const next = st.tabs[Math.min(i, st.tabs.length - 1)];
    st.activeTab = next ? next.id : null;
    if (next) activateTab(next.id);
  }
  renderTabs();
  saveSession();
}

/* ---------- reorder / cycle / rename ---------- */
let _dragTabId = null;

function moveTabTo(dragId, targetId) {
  if (!dragId || dragId === targetId) return;
  const from = st.tabs.findIndex((t) => t.id === dragId);
  const to = st.tabs.findIndex((t) => t.id === targetId);
  if (from < 0 || to < 0) return;
  const [tab] = st.tabs.splice(from, 1);
  st.tabs.splice(to, 0, tab);
  renderTabs();
  saveSession();
}

function cycleTab(delta) {
  if (!st.tabs.length) return;
  const i = Math.max(0, st.tabs.findIndex((t) => t.id === st.activeTab));
  const next = st.tabs[(i + delta + st.tabs.length) % st.tabs.length];
  activateTab(next.id);
}

/* Ctrl+Shift+PgUp/PgDn — shift the active tab left/right in the order */
function moveActiveTab(delta) {
  const i = st.tabs.findIndex((t) => t.id === st.activeTab);
  if (i < 0) return;
  const j = i + delta;
  if (j < 0 || j >= st.tabs.length) return;
  const [tab] = st.tabs.splice(i, 1);
  st.tabs.splice(j, 0, tab);
  renderTabs();
  saveSession();
}

function renameChatPrompt(chatId) {
  const cs = st.chats[chatId];
  promptModal("Rename chat", "", cs?.chat?.title || "", async (v) => {
    const t = v.trim();
    if (!t) return;
    const res = await Api.call("chat_set_title", chatId, t);
    if (!res.ok) { toast(res.error, "err"); return; }
    if (cs?.chat) cs.chat.title = res.data.title;
    renderTabs();
    refreshArchiveTab();
  }, "Rename");
}

function tabCtxMenu(e, tab) {
  e.preventDefault();
  const items = [];
  if (tab.type === "chat") {
    items.push({ label: "Rename…", fn: () => renameChatPrompt(tab.chatId) }, "-");
  }
  items.push({ label: "Close tab", fn: () => closeTab(tab.id) });
  ctxMenu(e.clientX, e.clientY, items);
}

function renderTabs() {
  const strip = $("#tabstrip");
  strip.replaceChildren();
  // once tabs hit their min width the strip scrolls: the wheel drives it
  strip.onwheel = (e) => {
    if (!e.deltaY || e.deltaX) return;
    e.preventDefault();
    strip.scrollLeft += e.deltaY;
  };
  for (const tab of st.tabs) {
    const meta = TAB_META[tab.type];
    const live = (tab.type === "chat" && st.chats[tab.chatId]?.running)
      || (tab.type === "term" && st.terms[tab.chatId]?.running);
    const dirty = tab.type === "library" && st.lib.dirty;
    const node = el("div", {
      class: "tab" + (tab.id === st.activeTab ? " active" : ""),
      title: meta.title(tab),
      draggable: "true",
      onclick: () => activateTab(tab.id),
      onauxclick: (e) => { if (e.button === 1) { e.preventDefault(); closeTab(tab.id); } },
      oncontextmenu: (e) => tabCtxMenu(e, tab),
      ondblclick: () => { if (tab.type === "chat") renameChatPrompt(tab.chatId); },
    },
      el("span", { html: icon(meta.icon, 13) }),
      el("span", { class: "tab-name", text: meta.title(tab) }),
      live ? el("span", { class: "tab-live", text: "●" }) : null,
      dirty ? el("span", { class: "tab-dirty", text: "●" }) : null,
      el("button", {
        class: "tab-x", text: "×", title: "Close tab",
        onclick: (e) => { e.stopPropagation(); closeTab(tab.id); },
      }));
    // drag-to-reorder: the move happens on drop (re-rendering the strip
    // mid-drag would cancel the browser's drag operation)
    node.addEventListener("dragstart", (e) => {
      _dragTabId = tab.id;
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", tab.id); } catch (err) { /* ok */ }
    });
    node.addEventListener("dragover", (e) => {
      if (!_dragTabId || _dragTabId === tab.id) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      node.classList.add("drop-target");
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop-target"));
    node.addEventListener("drop", (e) => {
      e.preventDefault();
      node.classList.remove("drop-target");
      moveTabTo(_dragTabId, tab.id);
      _dragTabId = null;
    });
    node.addEventListener("dragend", () => {
      _dragTabId = null;
      for (const n of strip.querySelectorAll(".drop-target")) {
        n.classList.remove("drop-target");
      }
    });
    strip.append(node);
  }
  strip.querySelector(".tab.active")
    ?.scrollIntoView({ block: "nearest", inline: "nearest" });
}
