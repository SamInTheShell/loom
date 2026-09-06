/* picker.js - the first screen: the Loom logo, create/select library, and
 * the recent-libraries list (clearable; entries right-clickable to clear
 * one or omit it permanently). */
"use strict";

function showPicker() {
  $("#app").classList.add("hidden");
  const p = $("#picker");
  p.classList.remove("hidden");
  renderPicker();
}

function renderPicker() {
  const p = $("#picker");
  p.replaceChildren();

  // frameless: the picker needs its own drag strip + window controls
  if (st.frameless) {
    const strip = el("div", { class: "picker-drag" }, windowControls());
    wireDragRegion(strip, (e) => !e.target.closest("button"));
    p.append(strip);
  }

  p.append(
    el("div", { class: "picker-logo" },
      el("span", { html: `<svg viewBox="0 0 16 16" width="44" height="44" aria-hidden="true">
        <g stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none">
          <path d="M5.5 2v4.6M5.5 8.9V14"/><path d="M10.5 2v3.1M10.5 7.4V14"/>
          <path d="M2 6h7.1M12 6h2"/><path d="M2 10h2.1M7 10h7"/>
        </g></svg>` }),
      el("b", { text: "Loom" })),
    el("div", { class: "picker-sub", text: "Pick a library - a folder holding your prompts, knowledge, containers and loom.yaml." }),
    el("div", { class: "picker-actions" },
      el("button", { class: "btn btn-acc", text: "Create a library", onclick: pickerCreate }),
      el("button", { class: "btn", text: "Select a library", onclick: pickerSelect })));

  const box = el("div", { class: "picker-recents" });
  box.append(el("div", { class: "picker-recents-head" },
    el("span", { text: "Recent libraries" }),
    el("button", {
      class: "btn btn-sm", text: "Clear list",
      onclick: async () => {
        const d = await Api.get("recents_clear");
        st.recents = d.recents;
        renderPicker();
      },
    })));
  if (!st.recents.length) {
    box.append(el("div", { class: "picker-empty", text: "Nothing recent yet." }));
  }
  for (const r of st.recents) {
    const row = el("div", {
      class: "recent-row",
      tabindex: "0",
      onclick: () => openLibrary(r.path),
      onkeydown: (e) => {
        if (e.key === "Enter") { e.preventDefault(); openLibrary(r.path); }
        else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          const sib = e.key === "ArrowDown"
            ? e.currentTarget.nextElementSibling
            : e.currentTarget.previousElementSibling;
          sib?.focus?.();
        }
      },
      oncontextmenu: (e) => {
        e.preventDefault();
        ctxMenu(e.clientX, e.clientY, [
          { label: "Open", fn: () => openLibrary(r.path) },
          "-",
          { label: "Remove from list", fn: () => dropRecent(r.path, false) },
          {
            label: "Omit permanently", danger: true,
            fn: () => confirmModal("Omit from recents",
              "This folder will never show up in the recents list again - " +
              "opening it will always need a manual Select. Omit it?",
              "Omit", () => dropRecent(r.path, true), true),
          },
        ]);
      },
    },
      el("span", { class: "recent-name", text: baseName(r.path) }),
      el("span", { class: "recent-path", text: r.display || r.path, title: r.path }),
      (() => { const t = tago(r.lastOpened); t.className = "recent-when"; return t; })());
    box.append(row);
  }
  p.append(box);
  // keyboard-first: land on the most recent library - Enter opens it,
  // arrows walk the list, Tab reaches Create/Select
  setTimeout(() => p.querySelector(".recent-row")?.focus(), 0);
}

async function dropRecent(path, omit) {
  const d = await Api.get("recent_remove", path, omit);
  st.recents = d.recents;
  renderPicker();
}

async function pickerCreate() {
  const d = await Api.get("pick_folder");
  if (!d.path) return;
  const tl = await Api.call("templates_list");
  const templates = tl.ok ? tl.data.templates : [];
  if (!templates.length) { createFromTemplate(d.path, "starter"); return; }

  let chosen = templates.some((t) => t.id === "starter") ? "starter" : templates[0].id;
  const rows = templates.map((t) => {
    const radio = el("input", { type: "radio", name: "tpl", value: t.id });
    radio.checked = t.id === chosen;
    radio.addEventListener("change", () => { chosen = t.id; });
    return el("label", { class: "tpl-row" }, radio,
      el("span", { class: "tpl-body" },
        el("b", { text: t.name + (t.builtin ? "" : "  (yours)") }),
        el("span", { class: "tpl-desc", text: t.description || "" })));
  });
  modal("Pick a template",
    [el("p", { text: "The template stamps out the new library's prompts, knowledge and containers. Your own templates live in ~/.loom/templates/<name>/." }),
     el("div", { class: "tpl-list" }, ...rows)],
    [
      { label: "Cancel" },
      { label: "Create library", cls: "btn-acc", fn: () => createFromTemplate(d.path, chosen) },
    ], { id: "tpl-pick" });
}

async function createFromTemplate(path, templateId) {
  const res = await Api.call("library_create", path, templateId);
  if (!res.ok) { toast(res.error, "err"); return; }
  enterLibrary(res.data);
}

async function pickerSelect() {
  const d = await Api.get("pick_folder");
  if (!d.path) return;
  openLibrary(d.path);
}

async function openLibrary(path) {
  const res = await Api.call("library_open", path);
  if (!res.ok) { toast(res.error, "err"); return; }
  enterLibrary(res.data);
}

/* switch from the picker into the app shell, restoring the saved session
 * - tabs (and order), active tab, the Library tab's open file, tree
 * expansion and panel width - so the library reopens where it was left */
async function enterLibrary(data) {
  st.library = data.library;
  st.config = data.config;
  st.providers = {};
  st.reasoning = {};
  st.pins = [];
  // reasoning prefs + pins feed the composer button and the model menu
  Api.call("reasoning_all").then((r) => {
    if (r.ok) {
      st.reasoning = r.data.reasoning || {};
      refreshChatModelSelectors();
    }
  });
  Api.call("pins_all").then((r) => {
    if (r.ok) st.pins = r.data.pins || [];
  });
  $("#picker").classList.add("hidden");
  $("#app").classList.remove("hidden");
  const ln = $("#lib-name");
  ln.textContent = baseName(st.library);
  ln.title = st.library;
  if (st.config && st.config.error) {
    toast("loom.yaml: " + st.config.error, "warn", 7000);
  }

  st.tabs = [];
  st.activeTab = null;
  st.chats = {};
  $("#tabpanels").replaceChildren();
  $("#tabstrip").replaceChildren();

  // per-library editor state: reset, then overlay the saved session
  const sess = data.session || null;
  st.lib.open = null;
  st.lib.dirty = false;
  st.lib.expanded = (sess?.lib?.expanded && typeof sess.lib.expanded === "object")
    ? sess.lib.expanded : {};
  // first visit to this library: the tree opens QUIET - only
  // documentation/ expanded (applied once the tree has loaded)
  st.lib.seedCollapse = !!data.firstOpen;
  st.lib.leftWidth = Number(sess?.lib?.leftWidth) || null;
  st.lib.showHidden = !!sess?.lib?.showHidden;
  st.lib.scrollPos = 0;
  st.lib.restoreScroll = Number(sess?.lib?.scroll) || null;

  st.restoring = true;
  try {
    // seed each chat's view state (scroll, history window) BEFORE opening
    // so the first render already lands where the user left it
    const view = (sess?.chatView && typeof sess.chatView === "object")
      ? sess.chatView : {};
    const seed = (chatId) => {
      const v = view[chatId];
      if (!v) return;
      const cs = chatState(chatId);
      cs.windowSize = Math.max(CHAT_WINDOW, Number(v.windowSize) || CHAT_WINDOW);
      // "at the bottom" restores AS the bottom - pixel positions drift
      // between sessions, bottom-ness doesn't
      if (v.atBottom !== false) cs.restoreScroll = Infinity;
      else if (Number.isFinite(Number(v.scroll))) cs.restoreScroll = Number(v.scroll);
      cs.atBottom = v.atBottom !== false;
      cs.follow = cs.atBottom;   // bottom-ness IS the stored intent
    };
    // terminal tab configs (shells don't survive a restart - the tab
    // reopens on its setup form, pre-filled)
    st.terms = {};
    const termCfgs = (sess?.terms && typeof sess.terms === "object") ? sess.terms : {};
    // chats the backend says were open at quit (archived=false)
    const openChats = new Set((data.openChats || []).map((c) => c.id));
    for (const t of sess?.tabs || []) {
      if (t.type === "chat") {
        if (t.chatId && openChats.has(t.chatId)) {
          seed(t.chatId);
          await openChat(t.chatId);
          openChats.delete(t.chatId);
        }
      } else if (t.type === "term") {
        if (t.chatId && termCfgs[t.chatId]) {
          const tc = termCfgs[t.chatId];
          st.terms[t.chatId] = {
            container: tc.container || "sandbox",
            folders: Array.isArray(tc.folders) ? tc.folders : [],
            network: netMode(tc.network), env: tc.env || "",
            title: tc.title || null,
            started: false, running: false,
          };
          openTab("term", t.chatId);
        }
      } else if (t.type === "diag") {
        if (t.chatId) openTab("diag", t.chatId);
      } else if (TAB_META[t.type]) {
        openTab(t.type);
      }
    }
    // open chats the session missed (older session, crash, ...): still tabs
    for (const id of openChats) {
      seed(id);
      await openChat(id);
    }

    if (sess?.lib?.open && tabById("library")) {
      await openLibFile(sess.lib.open);
    }
    if (sess?.activeTab && tabById(sess.activeTab)) {
      activateTab(sess.activeTab);
    }
    if (!st.tabs.length) {
      openTab("library");
      // first visit to this library: land on the documentation Welcome page
      if (data.firstOpen) setTimeout(() => openLibFile("documentation/welcome.md"), 50);
    }
  } finally {
    st.restoring = false;
  }
  saveSession();
  // navigation history is per library-session; seed it with where we landed
  resetNav();
  if (st.activeTab) {
    pushNav({ tab: st.activeTab,
              file: st.activeTab === "library" ? st.lib.open : null });
  }
}
