/* main.js — boot, event routing, keyboard. */
"use strict";

let _booted = false;

async function boot() {
  if (_booted) return;
  _booted = true;

  // topbar
  $("#btn-back").innerHTML = icon("chevleft");
  $("#btn-fwd").innerHTML = icon("chevright");
  $("#btn-back").addEventListener("click", () => navGo(-1));
  $("#btn-fwd").addEventListener("click", () => navGo(1));
  $("#btn-newchat").innerHTML = icon("chat");   // same glyph as chat tabs
  $("#btn-terminal").innerHTML = icon("terminal");
  $("#btn-library").innerHTML = icon("library");
  $("#btn-servers").innerHTML = icon("servers");
  $("#btn-models").innerHTML = icon("download");
  $("#btn-archive").innerHTML = icon("archive");
  $("#btn-envs").innerHTML = icon("key");
  $("#btn-switchlib").innerHTML = icon("swap");
  $("#btn-newchat").addEventListener("click", () => newChat());
  $("#btn-terminal").addEventListener("click", () => newTerminal());
  $("#btn-library").addEventListener("click", () => openTab("library"));
  // the library icon is a DRAG SOURCE: drop it on a chat's composer to
  // attach the whole library folder (read-only; the pill can flip it to
  // write for self-improvement work on prompts/knowledge/tools)
  const libBtn = $("#btn-library");
  libBtn.draggable = true;
  libBtn.addEventListener("dragstart", (e) => {
    if (!st.library) { e.preventDefault(); return; }
    // custom dataTransfer types don't survive the embedded webview's
    // drag pipeline — for a same-app drag the flag is the source of truth
    st.dragLibrary = true;
    e.dataTransfer.setData("text/plain", st.library);
    e.dataTransfer.effectAllowed = "copy";
  });
  libBtn.addEventListener("dragend", () => { st.dragLibrary = false; });
  $("#btn-servers").addEventListener("click", () => openTab("servers"));
  $("#btn-models").addEventListener("click", () => openTab("models"));
  $("#btn-archive").addEventListener("click", () => openTab("archive"));
  $("#btn-envs").addEventListener("click", () => openTab("envs"));
  // no Ctrl-reveal chips on the nav buttons — their hover tooltips
  // already spell out the shortcuts
  renderNotifBadge();
  $("#btn-notif").addEventListener("click", openAlertsTab);
  $("#btn-theme").addEventListener("click", async () => {
    st.theme = st.theme === "light" ? "dark" : "light";
    applyTheme();
    Api.call("set_theme", st.theme);
  });
  $("#btn-switchlib").addEventListener("click", switchLibraryFlow);

  try {
    const d = await Api.get("app_state");
    st.theme = d.theme || "dark";
    st.recents = d.recents || [];
    st.frameless = !!d.frameless;
  } catch (e) { /* toasted */ }
  applyTheme();
  setupFramelessChrome();
  installTooltips();
  installHotkeyReveal();
  // relative timestamps tick while the window sits open
  setInterval(() => {
    for (const n of document.querySelectorAll("[data-tago]")) {
      const t = timeAgo(Number(n.dataset.tago));
      if (n.textContent !== t) n.textContent = t;
    }
  }, 30000);
  showPicker();
}

/* ---------- styled tooltips for icon buttons ----------
 * Native title-tooltips in embedded Chromium are slow and plain; every
 * .iconbtn/.winbtn gets a styled one instead. Delegated: buttons created
 * later (picker controls, composer icons) are covered automatically. On
 * first hover the native `title` moves into data-tip so the browser one
 * never doubles up; dynamically re-set titles are re-absorbed the same way. */
function installTooltips() {
  let tip = null, timer = null, cur = null;
  const hide = () => {
    clearTimeout(timer);
    timer = null;
    tip?.remove();
    tip = null;
    cur = null;
  };
  document.addEventListener("mouseover", (e) => {
    const b = e.target.closest?.(".iconbtn, .winbtn");
    if (!b) return;
    if (b === cur) return;
    hide();
    cur = b;
    if (b.getAttribute("title")) {
      b.dataset.tip = b.getAttribute("title");
      b.removeAttribute("title");
    }
    const text = b.dataset.tip;
    if (!text) return;
    timer = setTimeout(() => {
      tip = el("div", { class: "tooltip", text });
      document.body.append(tip);
      const r = b.getBoundingClientRect();
      const tr = tip.getBoundingClientRect();
      const left = Math.max(6, Math.min(r.left + r.width / 2 - tr.width / 2,
        window.innerWidth - tr.width - 6));
      let top = r.bottom + 7;
      if (top + tr.height > window.innerHeight - 6) top = r.top - tr.height - 7;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }, 350);
  });
  document.addEventListener("mouseout", (e) => {
    if (cur && !cur.contains(e.relatedTarget)) hide();
  });
  document.addEventListener("mousedown", hide, true);
}

/* ---------- integrated titlebar (frameless window) ---------- */
function setupFramelessChrome() {
  if (!st.frameless) return;
  document.documentElement.classList.add("frameless");
  document.body.classList.add("frameless");
  $("#resize-zones").classList.remove("hidden");
  const wc = $("#topbar .win-controls");
  wc.classList.remove("hidden");
  $("#btn-min").innerHTML = icon("minus");
  $("#btn-max").innerHTML = icon("maxsq");
  $("#btn-close").innerHTML = icon("closex");
  $("#btn-min").addEventListener("click", () => Api.call("win_minimize"));
  $("#btn-max").addEventListener("click", () => Api.call("win_toggle_max"));
  $("#btn-close").addEventListener("click", () => Api.call("win_close"));

  // the top bar IS the titlebar: empty areas drag (compositor-native),
  // double-click toggles maximize; controls/buttons keep their clicks
  const isChrome = (e) =>
    !e.target.closest("button, select, input, a, textarea, .tab");
  wireDragRegion($("#topbar"), isChrome);

  for (const z of document.querySelectorAll("#resize-zones .rs")) {
    z.addEventListener("mousedown", (e) => {
      if (e.button === 0 && !st.maximized) {
        e.preventDefault();
        Api.call("win_resize", z.dataset.edges);
      }
    });
  }
}

/* Titlebar drag with a MOVEMENT THRESHOLD: startSystemMove on bare
 * mousedown hands the pointer to the compositor, which would eat the
 * second click of a double-click — so the system move only starts after
 * ~4px of actual motion, and dblclick reliably toggles maximize. */
function wireDragRegion(region, isChrome) {
  let down = null;
  region.addEventListener("mousedown", (e) => {
    if (e.button === 0 && isChrome(e)) down = { x: e.clientX, y: e.clientY };
  });
  region.addEventListener("mousemove", (e) => {
    if (!down) return;
    if (Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 4) {
      down = null;
      Api.call("win_drag");
    }
  });
  window.addEventListener("mouseup", () => { down = null; });
  region.addEventListener("dblclick", (e) => {
    if (isChrome(e)) Api.call("win_toggle_max");
  });
}

function applyWinState(maximized) {
  st.maximized = !!maximized;
  document.body.classList.toggle("maximized", st.maximized);
  const b = $("#btn-max");
  if (b) b.innerHTML = icon(st.maximized ? "restore" : "maxsq");
  const pb = $("#picker .winmax");
  if (pb) pb.innerHTML = icon(st.maximized ? "restore" : "maxsq");
}

/* window controls for surfaces without the topbar (the picker) */
function windowControls() {
  if (!st.frameless) return null;
  const mk = (ic, title, fn, cls) => {
    const b = el("button", { class: "iconbtn winbtn " + (cls || ""), title, html: icon(ic) });
    b.addEventListener("click", fn);
    return b;
  };
  return el("span", { class: "win-controls" },
    mk("minus", "Minimize", () => Api.call("win_minimize")),
    mk(st.maximized ? "restore" : "maxsq", "Maximize / restore",
      () => Api.call("win_toggle_max"), "winmax"),
    mk("closex", "Close to tray", () => Api.call("win_close"), "winclose"));
}

/* ---------- events from Python ---------- */
onLMEvent((ev) => {
  switch (ev.type) {
    case "srv": {
      for (const r of ev.servers || []) st.servers[r.id] = r;
      if (ev.event && ["running", "stopped", "error"].includes(ev.event.state)) {
        delete st.notes[ev.event.id];
      }
      if (st.activeTab === "servers") renderServersTab();
      refreshChatModelSelectors();   // ●/◐/○ markers track live state
      chatsOnSrvEvent();             // fire messages queued behind a start
      break;
    }
    case "note":
      st.notes[ev.id] = ev.msg;
      if (st.activeTab === "servers") renderServersTab();
      break;
    case "config":
      st.config = ev.config;
      if (st.activeTab === "servers") refreshServersTab();
      refreshChatModelSelectors();   // loom.yaml edits reach open chats too
      break;
    case "toast":
      toast(ev.msg, ev.level === "ok" ? "ok" : ev.level === "err" ? "err" : "warn");
      break;
    case "chat":
      onChatEvent(ev);
      break;
    case "term":
      onTermEvent(ev);
      break;
    case "models":
      if (ev.op === "download") onDownloaderEvent(ev);
      else onModelsEvent(ev);
      break;
    case "prompt":
      sshPromptModal(ev.id, ev.text);
      break;
    case "confirm_quit":
      confirmQuitModal(ev.servers || 0, ev.chats || 0, ev.terminals || 0);
      break;
    case "winstate":
      applyWinState(ev.maximized);
      break;
  }
});

/* ---------- switching libraries ---------- */
async function switchLibraryFlow() {
  const res = await Api.call("switch_blockers");
  const blk = res.ok ? res.data : { servers: [], chats: 0 };

  const back = async () => {
    await Api.call("library_close");   // stops servers + cancels chats
    st.tabs = [];
    st.activeTab = null;
    st.library = null;
    st.chats = {};
    st.servers = {};
    st.notes = {};
    st.reasoning = {};
    st.pins = [];
    if (st.lib.editor) { st.lib.editor.destroy(); st.lib.editor = null; }
    st.lib.open = null;
    st.lib.dirty = false;
    const d = await Api.get("recents_get");
    st.recents = d.recents;
    showPicker();
  };

  const consequences = [];
  if (blk.servers.length) {
    consequences.push(el("p", { text: "These llama-servers will be stopped:" }));
    consequences.push(el("ul", {},
      ...blk.servers.map((n) => el("li", { text: n }))));
  }
  if (blk.chats) {
    consequences.push(el("p", {
      text: blk.chats + " chat generation" + (blk.chats > 1 ? "s" : "")
        + " will be cancelled before completing.",
    }));
  }
  if (blk.terminals) {
    consequences.push(el("p", {
      text: blk.terminals + " terminal session"
        + (blk.terminals > 1 ? "s" : "") + " will be closed.",
    }));
  }
  if (st.lib.dirty) {
    consequences.push(el("p", {
      text: `Unsaved changes to "${st.lib.open}" will be discarded.`,
    }));
  }
  if (!consequences.length) { back(); return; }   // nothing at stake

  modal("Switch library?", consequences, [
    { label: "Cancel" },
    { label: "Switch library", cls: "btn-danger", fn: () => { back(); } },
  ], { id: "switch-lib" });
}

/* ---------- the alert badge (the bell opens the Alerts tab) ---------- */
function renderNotifBadge() {
  const b = $("#btn-notif");
  if (!b) return;
  b.innerHTML = icon("bell");
  b.classList.toggle("has-unseen", NotifLog.unseen > 0);
  const t = (NotifLog.unseen ? `Alerts (${NotifLog.unseen} new)` : "Alerts")
    + "  (Ctrl+Shift+A)";
  // installTooltips may have absorbed title into data-tip — keep both fresh
  if (b.dataset.tip) b.dataset.tip = t;
  else b.title = t;
}

function sshPromptModal(promptId, text) {
  const input = el("input", { type: "password" });
  const remember = el("input", { type: "checkbox" });
  modal("SSH needs a secret",
    [el("p", { text }), input,
     el("label", { class: "chk" }, remember, "Remember for this session")],
    [
      { label: "Cancel", fn: () => { Api.call("prompt_cancel", promptId); } },
      {
        label: "OK", cls: "btn-acc",
        fn: () => { Api.call("prompt_answer", promptId, input.value, remember.checked); },
      },
    ]);
  setTimeout(() => input.focus(), 0);
}

function confirmQuitModal(servers, chats, terminals) {
  const bits = [];
  if (servers) bits.push(servers + " running server" + (servers > 1 ? "s" : ""));
  if (chats) bits.push(chats + " streaming chat" + (chats > 1 ? "s" : ""));
  if (terminals) bits.push(terminals + " terminal session" + (terminals > 1 ? "s" : ""));
  // singleton: every close attempt pushes a confirm_quit event — repeated
  // clicks must replace this dialog, never stack another copy
  modal("Quit Loom?",
    [el("p", {
      text: "Quitting stops " + (bits.join(" and ") || "running work")
        + ". Servers die with the app.",
    })],
    [
      { label: "Cancel" },
      {
        label: "Quit", cls: "btn-danger",
        fn: () => {
          saveSessionNow();   // the debounced save must not lose the tail
          Api.call("quit_confirmed");
        },
      },
    ], { id: "confirm-quit" });
}

/* ---------- keyboard ----------
 * Hold Ctrl: every visible control reveals its key (hotkeys.js chips).
 * Ctrl+Shift+I  DevTools            Ctrl+N              new chat
 * Ctrl+T        new terminal        Ctrl+W              close tab
 * Ctrl+L / E / M / H   Library / Servers / Models / Archive
 * Ctrl+Shift+A  Alerts tab          Ctrl+Shift+E        Environments tab
 * Ctrl+1..9     jump to tab N
 * Ctrl+PgUp/PgDn        cycle tabs (also Ctrl+Tab)
 * Ctrl+Shift+PgUp/PgDn  move the active tab left/right
 * Ctrl+I        focus the message input
 * Ctrl+R        the chat's Retry / Continue banner (when showing)
 * Ctrl+. / Ctrl+, / Ctrl+; / Ctrl+'   the active chat's model /
 *               permission / environment / container menus
 * Ctrl+Shift+N  toggle the active chat's container network access
 * PgUp/PgDn     in the message input: scroll the chat thread
 * Ctrl+F        library: focus search    Ctrl+\   library: tree ↔ editor
 * Esc           close menu/dialog, else stop the active chat's generation
 * Ctrl+Enter / Ctrl+Esc   allow / deny a waiting permission request
 * Ctrl+S        save the library file (also works inside the editor)
 */
document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  // Ctrl/Cmd+Shift+I → the REAL Chromium DevTools (bounced to Python, which
  // attaches the local devtools page — no remote debugger involved)
  if (mod && e.shiftKey && (e.key === "I" || e.key === "i")) {
    e.preventDefault();
    Api.call("open_devtools");
    return;
  }
  // Alt+Left / Alt+Right (and media-key equivalents) — navigation history
  if (st.library && e.altKey && !mod && !e.shiftKey) {
    if (e.key === "ArrowLeft") { e.preventDefault(); navGo(-1); return; }
    if (e.key === "ArrowRight") { e.preventDefault(); navGo(1); return; }
  }
  if (st.library && (e.key === "BrowserBack" || e.key === "BrowserForward")) {
    e.preventDefault();
    navGo(e.key === "BrowserBack" ? -1 : 1);
    return;
  }
  const anyModal = !!$("#modal-root").firstChild;
  const anyMenu = !!$("#ctx-root").firstChild;
  // the active chat's permission gate: Ctrl+Enter allows, Ctrl+Esc denies —
  // but never while a dialog or menu sits on top of it
  if (st.library && mod && !e.shiftKey && !e.altKey && !anyModal && !anyMenu
      && (e.key === "Enter" || e.key === "Escape")) {
    const active = tabById(st.activeTab);
    const callId = active?.type === "chat" ? waitingToolId(active.chatId) : null;
    if (callId) {
      e.preventDefault();
      Api.call("tool_answer", callId, e.key === "Enter" ? "allow" : "deny");
      return;
    }
  }
  if (st.library && mod && !e.altKey) {
    const k = e.key.toLowerCase();
    // inside a terminal, single-letter Ctrl combos belong to the SHELL
    // (Ctrl+L clear, Ctrl+N next-line, Ctrl+W delete-word…); inside the
    // editor, Ctrl+B/I/E/… are formatting. Ctrl+T and tab keys stay ours.
    const inTerm = !!(e.target.closest && e.target.closest(".term-emu"));
    const inEditor = !!(e.target.closest && e.target.closest(".md-surface"));
    if (k === "t" && !e.shiftKey) {
      e.preventDefault();
      newTerminal();
      return;
    }
    if (k === "n" && !e.shiftKey && !inTerm) {
      e.preventDefault();
      newChat();
      return;
    }
    if (k === "w" && !e.shiftKey && !inTerm) {
      e.preventDefault();
      if (st.activeTab) closeTab(st.activeTab);
      return;
    }
    if (/^[1-9]$/.test(e.key)) {
      const tab = st.tabs[Number(e.key) - 1];
      if (tab) { e.preventDefault(); activateTab(tab.id); }
      return;
    }
    // Ctrl+\ in a terminal is SIGQUIT — the shell keeps it
    if (e.key === "\\" && !inTerm) {
      if (st.activeTab === "library") { e.preventDefault(); libToggleFocus(); }
      return;
    }
    // Ctrl+. , ; '  — the active chat's model / permission / environment /
    // container menus (one adjacent key cluster)
    const MENU_KEYS = { ".": "model", ",": "perm", ";": "env", "'": "cont" };
    if (MENU_KEYS[e.key] && !e.shiftKey && !inTerm && !inEditor) {
      const active = tabById(st.activeTab);
      if (active?.type === "chat") {
        const btn = panelFor(active.id)?.querySelector(
          `[data-role="${MENU_KEYS[e.key]}"]`);
        if (btn) { e.preventDefault(); btn.click(); }
      }
      return;
    }
    if (!e.shiftKey && !inTerm && !inEditor) {
      if (k === "l") { e.preventDefault(); openTab("library"); return; }
      if (k === "e") { e.preventDefault(); openTab("servers"); return; }
      if (k === "m") { e.preventDefault(); openTab("models"); return; }
      if (k === "h") { e.preventDefault(); openTab("archive"); return; }
      if (k === "i") { e.preventDefault(); focusMessageInput(); return; }
      // Ctrl+R — the Retry/Continue banner, whenever it is showing
      if (k === "r") {
        const active = tabById(st.activeTab);
        if (active?.type === "chat") {
          e.preventDefault();   // never let the webview reload instead
          panelFor(active.id)?.querySelector('[data-role="resume"]')?.click();
        }
        return;
      }
      if (k === "f" && st.activeTab === "library") {
        e.preventDefault();
        st.lib.ui?.searchIn?.focus();
        return;
      }
    }
    if (e.shiftKey && k === "a") { e.preventDefault(); openAlertsTab(); return; }
    // Ctrl+Shift+E — the Environments tab
    if (e.shiftKey && k === "e") { e.preventDefault(); openTab("envs"); return; }
    // Ctrl+Shift+N — toggle container network access for the active chat
    if (e.shiftKey && k === "n") {
      const active = tabById(st.activeTab);
      const btn = active?.type === "chat"
        ? panelFor(active.id)?.querySelector('[data-role="net"]') : null;
      if (btn) { e.preventDefault(); btn.click(); }
      return;
    }
    if (e.key === "PageDown") {
      e.preventDefault();
      e.shiftKey ? moveActiveTab(1) : cycleTab(1);
      return;
    }
    if (e.key === "PageUp") {
      e.preventDefault();
      e.shiftKey ? moveActiveTab(-1) : cycleTab(-1);
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      cycleTab(e.shiftKey ? -1 : 1);
      return;
    }
    // Ctrl+S anywhere in the Library tab saves the open file; inside the
    // editor surface its own handler already does (don't save twice)
    if (k === "s" && !e.shiftKey && !inEditor) {
      if (st.activeTab === "library" && st.lib.dirty) {
        e.preventDefault();
        saveLibFile();
      }
      return;
    }
  }
  if (e.key === "Escape") {
    if (anyMenu) { e.preventDefault(); closeCtxTop(); return; }
    if (closeTopModal()) { e.preventDefault(); return; }
    // nothing stacked on top: Esc cancels the active chat's generation
    if (!mod && st.library) {
      const active = tabById(st.activeTab);
      if (active?.type === "chat" && st.chats[active.chatId]?.running
          && !(e.target.closest && e.target.closest(".term-emu"))) {
        e.preventDefault();
        stopChatGeneration(active.chatId);
      }
    }
  }
}, true);   // capture: a focused editor must not swallow the DevTools key

/* a drop anywhere else must never navigate the page away */
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => e.preventDefault());

/* anchors (chat markdown renders <a href>) must never navigate the
 * webview either: external URIs go to the OS after confirmation, bare
 * targets open as library documents */
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a) return;
  e.preventDefault();
  const href = a.getAttribute("href") || "";
  if (!href || href.startsWith("#")) return;
  if (isAbsoluteUri(href)) confirmOpenExternal(href);
  else if (st.library) openLibraryFileAt(resolveLibLink(href) || href);
}, true);

/* mouse back/forward buttons (X1/X2) drive the navigation history */
window.addEventListener("mouseup", (e) => {
  if (!st.library) return;
  if (e.button === 3) { e.preventDefault(); navGo(-1); }
  else if (e.button === 4) { e.preventDefault(); navGo(1); }
});
window.addEventListener("auxclick", (e) => {
  if (e.button === 3 || e.button === 4) e.preventDefault();
});

/* pywebview fires this when the bridge is ready; plain browsers never do. */
window.addEventListener("pywebviewready", boot);
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => { if (!window.pywebview) boot(); }, 1200);
});
