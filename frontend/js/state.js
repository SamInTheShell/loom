/* state.js - the one global state object. Views read it, events mutate it. */
"use strict";

const st = {
  theme: "dark",
  library: null,        // absolute path of the open library, or null
  config: null,         // parsed loom.yaml ({error} when it doesn't parse)
  recents: [],

  tabs: [],             // [{id, type: 'library'|'servers'|'archive'|'chat', chatId?}]
  activeTab: null,      // tab id

  chats: {},            // chatId -> {chat, running, live:{text,think,tools:{}}, pendingTool}
  terms: {},            // termId -> {container, folders, network, title, started, running}
  providers: {},        // provider name -> {state, detail, models:[{id,ctx}], ts}
  reasoning: {},        // "provider::model" -> {method, level} reasoning prefs
  pins: [],             // pinned "provider::model" keys - listed first

  lib: {                // library tab state
    tree: [],
    configFile: "loom.yaml",
    open: null,         // rel of the open file
    dirty: false,
    editor: null,       // LoomEditor instance
    searching: false,
    expanded: {},       // rel -> bool
    leftWidth: null,    // tree panel width (px), persisted in the session
  },

  restoring: false,     // true while enterLibrary replays a saved session
  dragLibrary: false,   // a topbar library-icon drag is in flight
};

/* Persist "where I left off" for this library: tabs (and order), the
 * active tab, and the Library tab's open file / expansion / panel width.
 * Debounced; every structural UI change calls this. saveSessionNow is
 * the immediate form - quitting must not lose the last 400ms. */
function saveSessionNow() {
  if (!st.library || st.restoring) return;
  // per-chat view state: scroll position + how much history was loaded.
  // atBottom is the truth that survives restarts: "at the bottom" is a
  // STATE, not a pixel value - heights drift between sessions
  const chatView = {};
  for (const t of st.tabs) {
    if (t.type !== "chat") continue;
    const cs = st.chats[t.chatId];
    if (cs) {
      chatView[t.chatId] = {
        scroll: Math.round(cs.scrollPos || 0),
        atBottom: cs.atBottom !== false,
        windowSize: cs.windowSize || 200,
      };
    }
  }
  // terminal tab configs (the shell itself does not survive a restart -
  // the tab reopens on its setup form, pre-filled)
  const terms = {};
  for (const t of st.tabs) {
    if (t.type !== "term") continue;
    const tc = st.terms[t.chatId];
    if (tc) {
      terms[t.chatId] = { container: tc.container, folders: tc.folders,
                          network: netMode(tc.network), env: tc.env || "",
                          title: tc.title || null };
    }
  }
  Api.call("session_save", {
    tabs: st.tabs.map((t) => ({ type: t.type, chatId: t.chatId || null })),
    activeTab: st.activeTab,
    lib: {
      open: st.lib.open,
      expanded: st.lib.expanded,
      leftWidth: st.lib.leftWidth || null,
      scroll: Math.round(st.lib.scrollPos || 0),
      showHidden: !!st.lib.showHidden,
    },
    chatView,
    terms,
  });
}

const saveSession = debounce(saveSessionNow, 400);

function applyTheme() {
  document.documentElement.dataset.theme = st.theme === "light" ? "light" : "dark";
  const b = $("#btn-theme");
  if (b) b.innerHTML = icon(st.theme === "light" ? "moon" : "sun");
}

