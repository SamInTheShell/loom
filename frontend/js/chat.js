/* chat.js — chat tabs. The message area is modeled after llama.cpp's web
 * chat: a centered column thread, user bubbles, assistant text floating as
 * rendered markdown with a streaming caret, tool cards inline, and a
 * per-turn stats row (tok/s, ttft, tokens).
 *
 * Close semantics: closing a chat tab ARCHIVES the chat (reopen it from
 * the Chat Archive tab). A chat with inference running can't close until
 * the user confirms cancellation.
 */
"use strict";

const CHAT_WINDOW = 200;   // messages rendered/transferred per fetch

function chatState(chatId) {
  return st.chats[chatId] || (st.chats[chatId] = {
    chat: null, running: false,
    live: null,          // {text, think} while streaming
    tools: {},           // callId -> card state
    toolsOpen: {},       // callId -> user expanded a resolved card
    windowSize: CHAT_WINDOW,
    queue: [],           // [{text, images}] waiting to send
    histIdx: null,       // input history navigation position
    stats: null,
  });
}

async function newChat() {
  // Ctrl+N / the + button while a chat tab is active: the new chat is a
  // CLEAN context window with the same working setup — model, permission
  // mode, and folder attachments carry over; messages don't.
  let template = null;
  const active = tabById(st.activeTab);
  if (active?.type === "chat") {
    const src = st.chats[active.chatId]?.chat;
    if (src) {
      template = {
        model: src.model || "",
        permMode: src.permMode || "",
        network: !!src.network,
        folders: (src.folders || []).map((f) => ({ path: f.path, mode: f.mode })),
      };
    }
  }
  const res = await Api.call("chat_new", template);
  if (!res.ok) { toast(res.error, "err"); return; }
  const c = res.data.chat;
  chatState(c.id).chat = c;
  openTab("chat", c.id);
  renderChat(c.id);
}

async function openChat(chatId) {
  const res = await Api.call("chat_get", chatId, chatState(chatId).windowSize);
  if (!res.ok) { toast(res.error, "err"); return; }
  const cs = chatState(chatId);
  cs.chat = res.data.chat;
  cs.running = res.data.running;
  if (cs.chat.archived) {
    Api.call("chat_unarchive", chatId);
    cs.chat.archived = false;
  }
  openTab("chat", chatId);
  renderChat(chatId);
}

function closeChatTab(chatId) {
  const cs = st.chats[chatId];
  if (cs?.running) {
    confirmModal("Inference is running",
      "This chat is still generating. Closing it cancels the response " +
      "and archives the chat. Cancel and close?",
      "Cancel & close", async () => {
        await Api.call("chat_stop", chatId);
        await Api.call("chat_close", chatId, true);
        removeTab("chat:" + chatId);
      }, true);
    return;
  }
  Api.call("chat_close", chatId, false).then((res) => {
    if (!res.ok && res.needsConfirm) { closeChatTab(chatId); return; }  // raced into running
    removeTab("chat:" + chatId);
    refreshArchiveTab();
  });
}

/* ---------- mount ---------- */
function mountChatTab(panel, chatId) {
  panel.classList.add("chattab");
  const thread = el("div", { class: "chat-thread", "data-role": "thread" });

  // jump-to-bottom: appears (bottom left) whenever the user has scrolled
  // up — which is also exactly when auto-scroll is paused; clicking it
  // lands on the last message and re-arms auto-scroll
  const jump = el("button", {
    class: "jump-bottom", title: "Jump to the latest message",
    html: icon("down", 15),
  });
  jump.addEventListener("click", () => {
    thread.scrollTop = thread.scrollHeight;
    updateJump();
  });
  const updateJump = () => jump.classList.toggle("show", !atBottom(thread));
  thread.addEventListener("scroll", updateJump, { passive: true });
  thread.addEventListener("scroll", () => {
    // a hidden panel reads scrollTop 0 — never let that clobber the real
    // position (it's restored when the tab activates again)
    if (!thread.clientHeight) return;
    const cs = chatState(chatId);
    cs.scrollPos = thread.scrollTop;   // tab switches + session
    // BOTTOM-NESS is what tab switching must preserve: at-bottom means
    // "keep following the stream", scrolled-up means "hold my spot"
    cs.atBottom = atBottom(thread);
    saveSession();
  }, { passive: true });
  panel._updateJump = updateJump;
  const threadWrap = el("div", { class: "thread-wrap" }, thread, jump);

  const composer = el("div", { class: "composer" });
  const inner = el("div", { class: "composer-inner" });
  const attach = el("div", { class: "attach-bar", "data-role": "attach" });

  const input = el("textarea", {
    class: "compose-input", rows: "1",
    placeholder: "Ask anything…  (Enter to send · Shift+Enter for a newline · Ctrl+I focuses here)",
  });
  setHotkey(input, "Ctrl+I");
  const resizeInput = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
  };
  panel._resizeInput = resizeInput;
  input.addEventListener("input", () => {
    resizeInput();
    chatState(chatId).histIdx = null;   // an edit ends history navigation
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChatMessage(chatId);
      return;
    }
    // PgUp/PgDn from the input scroll the THREAD — the eyes are up there
    if ((e.key === "PageUp" || e.key === "PageDown")
        && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      thread.scrollTop += (e.key === "PageDown" ? 1 : -1)
        * Math.max(60, thread.clientHeight * 0.85);
      return;
    }
    // Up/Down at the very start of the input walk the message history
    if ((e.key === "ArrowUp" || e.key === "ArrowDown")
        && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
      if (handleInputHistory(chatId, input, e.key === "ArrowUp" ? -1 : 1)) {
        e.preventDefault();
      }
    }
  });

  const modelBtn = el("button", {
    class: "compose-model", "data-role": "model",
    title: "Model for this chat — click to pick, start or stop  (Ctrl+.)",
  });
  setHotkey(modelBtn, "Ctrl+.");
  modelBtn.addEventListener("click", () => modelMenu(modelBtn, chatId));

  const permBtn = el("button", {
    class: "perm-pill", "data-role": "perm",
    title: "Permission mode for this chat  (Ctrl+,)",
  });
  setHotkey(permBtn, "Ctrl+,");
  permBtn.addEventListener("click", () => permMenu(permBtn, chatId));

  const contBtn = el("button", {
    class: "perm-pill", "data-role": "cont",
    title: "Container for shell commands  (Ctrl+')",
  });
  setHotkey(contBtn, "Ctrl+'");
  contBtn.addEventListener("click", () => containerMenu(contBtn, chatId));

  const envBtn = el("button", {
    class: "perm-pill env-pill", "data-role": "env",
    title: "Environment loaded into shell containers  (Ctrl+;)",
  });
  setHotkey(envBtn, "Ctrl+;");
  envBtn.addEventListener("click", () => envMenu(envBtn, chatId));

  const ctxBtn = el("button", {
    class: "perm-pill ctx-chip", "data-role": "ctx",
    title: "Context usage — hover for the breakdown",
  });
  wireCtxHover(ctxBtn, chatId);

  const netBtn = el("button", {
    class: "perm-pill net-chip", "data-role": "net",
    title: "Container network access for shell commands — OFF by default",
  });
  setHotkey(netBtn, "Ctrl+Shift+N");
  netBtn.addEventListener("click", async () => {
    const cs = chatState(chatId);
    const res = await Api.call("chat_set_network", chatId, !cs.chat.network);
    if (!res.ok) { toast(res.error, "err"); return; }
    cs.chat.network = res.data.network;
    renderNetChip(chatId);
    refreshChatSilently(chatId);   // the system prompt env note changed
  });

  const btnImg = el("button", { class: "iconbtn", title: "Attach images (png/jpeg)", html: icon("image") });
  btnImg.addEventListener("click", async () => {
    const d = await Api.get("pick_images");
    const cs = chatState(chatId);
    cs.pendingImages = (cs.pendingImages || []).concat(d.paths || []);
    renderAttachBar(chatId);
  });
  const btnDir = el("button", { class: "iconbtn", title: "Attach a folder (view mode; toggle to write on the pill)", html: icon("folder") });
  btnDir.addEventListener("click", async () => {
    const d = await Api.get("pick_folder");
    if (d.path) attachFolderPath(chatId, d.path);
  });

  const btnSend = el("button", { class: "btn-send", "data-role": "send", title: "Send", html: icon("up") });
  btnSend.addEventListener("click", () => {
    if (!stopChatGeneration(chatId)) sendChatMessage(chatId);
  });

  // one integrated panel, llama.cpp-web-chat style: attachments, the
  // permission-mode pill (top right), the textarea, and the control row
  // all live inside the same rounded card. The context chip sits in the
  // control row, immediately LEFT of the model selector.
  const box = el("div", { class: "composer-panel" },
    el("div", { class: "panel-top" }, attach, envBtn, contBtn, permBtn),
    input,
    el("div", { class: "compose-controls" },
      btnImg, btnDir,
      el("span", { class: "spacer" }),
      ctxBtn, netBtn, modelBtn, btnSend));
  box.addEventListener("click", (e) => {
    if (e.target === box) input.focus();   // dead space focuses the input
  });

  // drag & drop images from the file system onto the input panel.
  // Browser drops carry file BYTES, not paths — each image is staged to
  // ~/.loom/attachments through the bridge and attached by path.
  const IMG_EXT = /\.(png|jpe?g|webp|gif|bmp)$/i;
  box.addEventListener("dragover", (e) => {
    e.preventDefault();
    // the library-icon drag advertises effectAllowed "copy" — the drop is
    // refused unless the target's dropEffect agrees
    if (st.dragLibrary) e.dataTransfer.dropEffect = "copy";
    box.classList.add("drag-over");
  });
  box.addEventListener("dragleave", () => box.classList.remove("drag-over"));
  box.addEventListener("drop", async (e) => {
    e.preventDefault();
    box.classList.remove("drag-over");
    // the topbar library icon dragged in: attach the whole library folder
    // read-only — the pill's view/write toggle opens it up for the model
    // to work on its own prompts/knowledge/tools
    const isLibraryDrag = st.dragLibrary
      || (st.library && e.dataTransfer?.getData("text/plain") === st.library);
    st.dragLibrary = false;
    if (isLibraryDrag && st.library) {
      attachFolderPath(chatId, st.library);
      return;
    }
    const cs = chatState(chatId);
    // OS drags carry file:// URIs — the only drop form with REAL PATHS.
    // A directory attaches as a folder mount; an image file attaches by
    // path (no byte round-trip needed).
    const uris = (e.dataTransfer?.getData("text/uri-list") || "")
      .split(/\r?\n/).map((u) => u.trim())
      .filter((u) => u && !u.startsWith("#") && u.startsWith("file://"));
    let handled = false;
    for (const u of uris) {
      const path = decodeURIComponent(u.replace(/^file:\/\/[^/]*/, ""));
      if (!path) continue;
      const res = await Api.call("path_kind", path);
      if (!res.ok) continue;
      if (res.data.kind === "dir") {
        handled = true;
        attachFolderPath(chatId, path);   // view mode; pill flips to write
      } else if (res.data.kind === "file" && IMG_EXT.test(path)) {
        handled = true;
        cs.pendingImages = (cs.pendingImages || []).concat([path]);
        renderAttachBar(chatId);
      } else if (res.data.kind === "file") {
        handled = true;
        toast(baseName(path) + " — single files don't attach; drop its "
          + "FOLDER to let the model read it, or drop images.", "warn");
      }
    }
    // the qt backend RECORDS the native paths of this drop (see
    // drop_paths in app.py) — that is the only reliable source of real
    // filesystem paths, and real paths are what folder attachment needs
    if ((e.dataTransfer?.files || []).length && !handled) {
      const r = await Api.call("drop_paths");
      for (const p of (r.ok && r.data.paths) || []) {
        const res = await Api.call("path_kind", p);
        if (!res.ok) continue;
        handled = true;
        if (res.data.kind === "dir") {
          attachFolderPath(chatId, p);   // a FOLDER drop is an attachment
        } else if (res.data.kind === "file" && IMG_EXT.test(p)) {
          cs.pendingImages = (cs.pendingImages || []).concat([p]);
          renderAttachBar(chatId);
        } else if (res.data.kind === "file") {
          toast(baseName(p) + " — single files don't attach; drop its "
            + "FOLDER to let the model read it, or drop images.", "warn");
        }
      }
    }
    if (handled) return;
    for (const f of e.dataTransfer?.files || []) {
      // pathless drop (e.g. an image dragged out of a web page): the
      // byte-based staging fallback
      if (!(f.type?.startsWith("image/") || IMG_EXT.test(f.name))) {
        toast(f.name + " — drop a folder to attach it, or images "
          + "(png/jpeg/webp/gif/bmp).", "warn");
        continue;
      }
      const reader = new FileReader();
      reader.onload = async () => {
        const r = await Api.call("stage_image", f.name, reader.result);
        if (!r.ok) { toast(r.error, "err"); return; }
        cs.pendingImages = (cs.pendingImages || []).concat([r.data.path]);
        renderAttachBar(chatId);
      };
      reader.readAsDataURL(f);
    }
  });
  // queued messages live in a visible panel ABOVE the input box
  const queueEl = el("div", { class: "send-queue", "data-role": "queue" });
  inner.append(queueEl, box);
  composer.append(inner);
  panel.append(threadWrap, composer);
  panel._input = input;
  ensureGitPoll();
  ensureLiveTicker();

  renderChat(chatId);
  renderQueue(chatId);
}

function chatPanel(chatId) { return panelFor("chat:" + chatId); }

/* ---------- git branch badges on folder pills ----------
 * Attached folders that are git worktrees show "@ <branch>"; a light poll
 * keeps the active chat's badges honest when the user checks out a
 * different branch OUTSIDE the app (the backend only reads .git/HEAD). */
const GIT_BADGE_POLL_MS = 5000;
let _gitPollTimer = null;

function ensureGitPoll() {
  if (_gitPollTimer) return;
  _gitPollTimer = setInterval(() => {
    const tab = tabById(st.activeTab);
    if (tab?.type === "chat") updateGitBadges(tab.chatId);
  }, GIT_BADGE_POLL_MS);
}

async function updateGitBadges(chatId) {
  const panel = chatPanel(chatId);
  const paths = (st.chats[chatId]?.chat?.folders || []).map((f) => f.path);
  if (!panel || !paths.length) return;
  const res = await Api.call("git_info", paths);
  if (!res.ok) return;
  const branches = res.data.branches || {};
  for (const b of panel.querySelectorAll("[data-branch-for]")) {
    const name = branches[b.dataset.branchFor];
    const text = name ? "@ " + name : "";
    if (b.textContent !== text) b.textContent = text;   // patch, don't churn
  }
}

/* attach a host folder to a chat (view mode unless told otherwise) —
 * the folder button, and drops of the library icon, both land here */
async function attachFolderPath(chatId, path, mode) {
  const cs = chatState(chatId);
  const folders = (cs.chat?.folders || []).slice();
  if (folders.some((f) => f.path === path)) {
    toast(baseName(path) + " is already attached.", "warn");
    return;
  }
  folders.push({ path, mode: mode === "write" ? "write" : "view" });
  const res = await Api.call("chat_set_folders", chatId, folders);
  if (!res.ok) { toast(res.error, "err"); return; }
  cs.chat.folders = res.data.folders;
  renderAttachBar(chatId);
  refreshChatSilently(chatId);   // mounts/tools changed
}

/* ---------- rendering ---------- */
function renderAttachBar(chatId) {
  const panel = chatPanel(chatId);
  if (!panel) return;
  const bar = panel.querySelector('[data-role="attach"]');
  const cs = chatState(chatId);
  bar.replaceChildren();
  for (const f of cs.chat?.folders || []) {
    const pill = el("span", { class: "pill", title: f.path },
      el("span", { html: icon("folder", 12) }),
      el("span", { class: "pname", text: baseName(f.path) }),
      el("span", { class: "pill-branch", "data-branch-for": f.path }),
      el("button", {
        class: "mode" + (f.mode === "write" ? " write" : ""),
        text: f.mode === "write" ? "write" : "view",
        title: "Toggle view / write access",
        onclick: async () => {
          f.mode = f.mode === "write" ? "view" : "write";
          const res = await Api.call("chat_set_folders", chatId, cs.chat.folders);
          if (res.ok) {
            cs.chat.folders = res.data.folders;
            renderAttachBar(chatId);
            refreshChatSilently(chatId);
          }
        },
      }),
      el("button", {
        text: "×", title: "Detach folder",
        onclick: async () => {
          cs.chat.folders = cs.chat.folders.filter((x) => x !== f);
          await Api.call("chat_set_folders", chatId, cs.chat.folders);
          renderAttachBar(chatId);
          refreshChatSilently(chatId);
        },
      }));
    bar.append(pill);
  }
  for (const p of cs.pendingImages || []) {
    const pill = el("span", { class: "pill", title: p },
      el("span", { html: icon("image", 12) }),
      el("span", { class: "pname", text: baseName(p) }),
      el("button", {
        text: "×", title: "Remove image",
        onclick: () => {
          cs.pendingImages = cs.pendingImages.filter((x) => x !== p);
          renderAttachBar(chatId);
        },
      }));
    wireImgPreview(pill, p);   // hover shows the actual image
    bar.append(pill);
  }
  // artifacts: what the model left in /artifacts for the user
  for (const a of cs.chat?.artifacts || []) {
    const isImg = !a.dir && /\.(png|jpe?g|webp|gif|bmp)$/i.test(a.name);
    const pill = el("span", {
      class: "pill artifact",
      title: (a.dir ? "folder — saves as a zip" : "file") + " · " + fmtBytes(a.bytes),
    },
      el("span", { html: icon(a.dir ? "box" : isImg ? "image" : "file", 12) }),
      el("span", { class: "pname", text: a.name }),
      el("button", {
        class: "mode", text: a.dir ? "zip" : "save",
        title: a.dir ? "Save this folder as a zip…" : "Save this file…",
        onclick: async () => {
          const res = await Api.call("artifact_save", chatId, a.name);
          if (!res.ok) { toast(res.error, "err"); return; }
          if (res.data.saved) toast("Saved " + res.data.saved, "ok");
        },
      }));
    if (isImg && a.path) wireImgPreview(pill, a.path);
    bar.append(pill);
  }
  updateGitBadges(chatId);   // fill "@ <branch>" on the folder pills
}

/* live server state for a model NAME (selector markers) */
function modelStateByName(name) {
  const m = (st.config?.models || []).find((x) => x.name === name);
  return m ? (st.servers[m.id]?.state || "stopped") : null;
}

function chatModelName(cs) {
  return cs?.chat?.model || modelNames()[0] || "";
}

function renderModelButton(chatId) {
  const panel = chatPanel(chatId);
  const btn = panel?.querySelector('[data-role="model"]');
  if (!btn) return;
  const name = chatModelName(st.chats[chatId]);
  const state = name ? modelStateByName(name) : null;
  // at-a-glance thinking level: the configured reasoning level's single
  // word (off/on/low/medium/xhigh/…); nothing shown on server default
  const mid = (st.config?.models || []).find((m) => m.name === name)?.id;
  const lvl = mid ? st.reasoning?.[mid]?.level : null;
  btn.replaceChildren(...[
    el("span", { class: "dot " + (state || "") }),
    el("span", { class: "mname", text: name || "no models" }),
    lvl ? el("span", { class: "mlvl", text: lvl,
                       title: "Reasoning: " + reasoningLabel(st.reasoning[mid]) }) : null,
    el("span", { class: "caret", text: "▾" }),
  ].filter(Boolean));
}

function renderNetChip(chatId) {
  const panel = chatPanel(chatId);
  const btn = panel?.querySelector('[data-role="net"]');
  if (!btn) return;
  const on = !!chatState(chatId).chat?.network;
  btn.classList.toggle("on", on);
  btn.replaceChildren(el("span", { text: on ? "network on" : "no network" }));
  btn.title = (on
    ? "Shell containers CAN reach the network in this chat — click to turn off"
    : "Shell containers run with --network=none — click to allow network")
    + "  (Ctrl+Shift+N)";
}

function activePermMode(cs) {
  return cs?.chat?.permMode
    || (st.config?.chat?.permission_mode) || "always-ask";
}

function renderPermPill(chatId) {
  const panel = chatPanel(chatId);
  const btn = panel?.querySelector('[data-role="perm"]');
  if (!btn) return;
  btn.replaceChildren(
    el("span", { html: icon("shield", 12) }),
    el("span", { text: activePermMode(st.chats[chatId]) }),
    el("span", { class: "caret", text: "▾" }));
}

/* ---------- the container pill (shell container per chat) ---------- */
function chatContainerName(cs) {
  return cs?.chat?.container || st.config?.containers?.default || "sandbox";
}

function renderContainerPill(chatId) {
  const btn = chatPanel(chatId)?.querySelector('[data-role="cont"]');
  if (!btn) return;
  const cs = chatState(chatId);
  const isDefault = !cs.chat?.container;
  btn.replaceChildren(
    el("span", { html: icon("servers", 12) }),
    el("span", { text: chatContainerName(cs) }),
    el("span", { class: "caret", text: "▾" }));
  btn.title = "Container for shell commands — "
    + (isDefault ? "the loom.yaml default" : "chosen for this chat");
}

function containerMenu(anchor, chatId) {
  const { close } = popupMenu(anchor, (m) => {
    const list = el("div", { class: "popup-list" });
    const cs = chatState(chatId);
    const defs = (st.config?.containers?.definitions || []).map((d) => d.name);
    const dflt = st.config?.containers?.default || "sandbox";
    const cur = cs.chat?.container || "";
    const pick = async (name) => {
      const res = await Api.call("chat_set_container", chatId, name);
      if (!res.ok) { toast(res.error, "err"); return; }
      cs.chat.container = res.data.container;
      renderContainerPill(chatId);
      refreshChatSilently(chatId);   // the env note names the container
      close();
    };
    list.append(el("div", {
      class: "popup-item" + (!cur ? " sel" : ""),
      onclick: () => pick(""),
    }, el("span", { class: "pi-name", text: "Default (" + dflt + ")" })));
    for (const name of defs) {
      list.append(el("div", {
        class: "popup-item" + (cur === name ? " sel" : ""),
        onclick: () => pick(name),
      },
        el("span", { class: "pi-name", text: name }),
        name === dflt ? el("span", { class: "pi-sub", text: "default" }) : null));
    }
    m.append(list);
  });
}

/* ---------- the environment pill (env vars for shell containers) ---------- */
function renderEnvPill(chatId) {
  const btn = chatPanel(chatId)?.querySelector('[data-role="env"]');
  if (!btn) return;
  const name = chatState(chatId).chat?.env || "";
  btn.classList.toggle("on", !!name);
  btn.replaceChildren(
    el("span", { html: icon("key", 12) }),
    el("span", { text: name || "no env" }),
    el("span", { class: "caret", text: "▾" }));
  btn.title = name
    ? "Environment '" + name + "' is loaded into shell containers"
    : "No environment — pick one to load its variables (API keys…) into "
      + "shell containers";
}

function envMenu(anchor, chatId) {
  const { menu, close } = popupMenu(anchor, (m) => {
    m.append(el("div", { class: "popup-list" }));
  });
  const list = menu.querySelector(".popup-list");
  const cs = chatState(chatId);
  const cur = cs.chat?.env || "";
  const pick = async (name) => {
    const res = await Api.call("chat_set_env", chatId, name);
    if (!res.ok) { toast(res.error, "err"); return; }
    cs.chat.env = res.data.env;
    renderEnvPill(chatId);
    refreshChatSilently(chatId);   // the env note lists the variables
    close();
  };
  Api.call("envs_list").then((r) => {
    const names = r.ok ? r.data.envs || [] : [];
    list.append(el("div", {
      class: "popup-item" + (!cur ? " sel" : ""),
      onclick: () => pick(""),
    }, el("span", { class: "pi-name", text: "No environment" })));
    for (const name of names) {
      list.append(el("div", {
        class: "popup-item" + (cur === name ? " sel" : ""),
        onclick: () => pick(name),
      }, el("span", { class: "pi-name", text: name })));
    }
    list.append(el("div", {
      class: "popup-item",
      onclick: () => { close(); openTab("envs"); },
    },
      el("span", { html: icon("key", 12) }),
      el("span", { class: "pi-name", text: "Manage environments…" })));
  });
}

/* refresh every open chat tab's model button (and a live popup) — called
 * on config edits and on every server state event */
function refreshChatModelSelectors() {
  for (const tab of st.tabs) {
    if (tab.type !== "chat") continue;
    renderModelButton(tab.chatId);
    renderPermPill(tab.chatId);
  }
  if (window._modelPopupRefresh) window._modelPopupRefresh();
}

/* ---------- the model popup: filter, refresh, pick, start/stop ----------
 * Every row also carries a brain button → that model's reasoning submenu:
 * first HOW to configure it (default / reasoning_effort request field /
 * enable_thinking template kwarg / think prompt switch), then the level
 * (low…max — or just on/off where that's all the method accepts). */
const REASONING_METHODS = [
  { id: "effort", name: "reasoning_effort",
    sub: "graded — Qwen 3.8: off/low/medium/xhigh · gpt-oss: low/medium/high",
    levels: ["off", "minimal", "low", "medium", "high", "xhigh", "max"] },
  { id: "template", name: "enable_thinking",
    sub: "boolean template kwarg (Qwen3-style) · on/off",
    levels: ["on", "off"] },
  { id: "prompt", name: "/think switch",
    sub: "prompt suffix · on/off", levels: ["on", "off"] },
];

function reasoningLabel(pref) {
  const m = REASONING_METHODS.find((x) => x.id === pref?.method);
  return m ? `${m.name}: ${pref.level}` : "server default";
}

function modelMenu(anchor, chatId) {
  let filter = "";
  let view = { mode: "list" };   // | {mode:"method"|"level", model, method}
  let reasonMap = st.reasoning || {};
  const { menu, close } = popupMenu(anchor, (m) => {
    const filterIn = el("input", {
      type: "text", placeholder: "Filter models…", spellcheck: "false",
    });
    filterIn.addEventListener("input", () => { filter = filterIn.value; render(); });
    filterIn.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(); }
    });
    const refreshBtn = el("button", {
      class: "iconbtn", title: "Re-probe server states", html: icon("refresh", 14),
      onclick: () => { Api.call("servers_refresh"); },
    });
    m.classList.add("model-popup");   // long model names need the room
    const head = el("div", { class: "popup-head" }, filterIn, refreshBtn);
    const list = el("div", { class: "popup-list" });
    m.append(head, list);
    m._list = list;
    m._head = head;
    setTimeout(() => filterIn.focus(), 0);
  });
  Api.call("reasoning_all").then((r) => {
    if (r.ok) {
      reasonMap = st.reasoning = r.data.reasoning || {};
      render();
    }
  });

  function matches(name) {
    const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
    const hay = name.toLowerCase();
    return terms.every((t) => hay.includes(t));
  }

  function render() {
    menu._head.classList.toggle("hidden", view.mode !== "list");
    if (view.mode === "list") renderList();
    else renderReason();
  }

  function renderList() {
    const cs = chatState(chatId);
    const current = chatModelName(cs);
    const list = menu._list;
    list.replaceChildren();
    // no models at all: this is the moment to offer the wizard
    if (!st.config?.models?.length) {
      list.append(
        el("div", { class: "popup-empty", text: "No models in loom.yaml yet." }),
        el("div", { class: "popup-cta" },
          el("button", {
            class: "btn btn-sm btn-acc", text: "New model…",
            title: "Find a GGUF and write the loom.yaml entry",
            onclick: () => { close(); modelWizard(); },
          })));
      return;
    }
    const pinnedSet = new Set(st.pins || []);
    const all = (st.config.models || []).filter((mo) => matches(mo.name));
    if (!all.length) {
      list.append(el("div", { class: "popup-empty", text: "No models match the filter." }));
      return;
    }
    // pinned models float to the top, both groups in loom.yaml order
    const models = [...all.filter((mo) => pinnedSet.has(mo.id)),
                    ...all.filter((mo) => !pinnedSet.has(mo.id))];
    for (const mo of models) {
      const state = st.servers[mo.id]?.state || "stopped";
      const busy = ["starting", "loading", "stopping"].includes(state);
      const up = ["running", "starting", "loading"].includes(state);
      const act = el("button", {
        class: "btn btn-sm" + (up ? " btn-danger" : ""),
        text: busy ? state + "…" : up ? "Stop" : "Start",
        disabled: busy ? "" : null,
        title: up ? "Stop this server" : "Start this server",
      });
      act.addEventListener("click", (e) => {
        e.stopPropagation();
        Api.call(up ? "server_stop" : "server_start", mo.id);
      });
      const pref = reasonMap[mo.id];
      const brain = el("button", {
        class: "iconbtn brain" + (pref ? " on" : ""),
        title: "Reasoning — " + reasoningLabel(pref) + " · click to configure",
        html: icon("brain", 14),
      });
      brain.addEventListener("click", (e) => {
        e.stopPropagation();
        view = { mode: "method", model: mo };
        render();
      });
      const isPin = pinnedSet.has(mo.id);
      const pinBtn = el("button", {
        class: "iconbtn pin" + (isPin ? " on" : ""),
        title: isPin ? "Unpin" : "Pin to the top",
        html: icon("pin", 13),
      });
      pinBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const r = await Api.call("pin_set", mo.id, !isPin);
        if (!r.ok) { toast(r.error, "err"); return; }
        st.pins = r.data.pins || [];
        renderList();
        if (st.activeTab === "servers") renderServersTab();
      });
      const row = el("div", {
        class: "popup-item" + (mo.name === current ? " sel" : ""),
        onclick: () => {
          const cs2 = chatState(chatId);
          cs2.chat.model = mo.name;
          Api.call("chat_set_model", chatId, mo.name)
            .then(() => refreshChatSilently(chatId));   // nCtx changed
          renderModelButton(chatId);
          close();
        },
      },
        el("span", { class: "dot " + state }),
        el("span", { class: "pi-col" },
          el("span", { class: "pi-name", text: mo.name, title: mo.model }),
          pref ? el("span", { class: "pi-reason",
            text: "reasoning · " + reasoningLabel(pref) }) : null),
        el("span", { class: "pi-sub", text: mo.host ? "ssh" : "" }),
        pinBtn, brain, act);
      list.append(row);
    }
  }

  /* the reasoning submenu, two layers deep */
  function renderReason() {
    const mo = view.model;
    const pref = reasonMap[mo.id] || null;
    const list = menu._list;
    list.replaceChildren();
    const item = (attrs, ...kids) => {
      const row = el("div", { class: "popup-item" + (attrs.sel ? " sel" : "") }, ...kids);
      row.addEventListener("click", attrs.onclick);
      list.append(row);
      return row;
    };
    const back = (label, fn) => item({ onclick: fn },
      el("span", { class: "pi-name", text: "← " + label }));
    const setPref = async (p) => {
      const res = await Api.call("reasoning_set", mo.id, p);
      if (!res.ok) { toast(res.error, "err"); return; }
      reasonMap = st.reasoning = res.data.reasoning || {};
      view = { mode: "list" };
      render();
      refreshChatModelSelectors();   // every composer button shows the level
    };

    if (view.mode === "method") {
      list.append(el("div", { class: "popup-empty",
        text: "Reasoning — " + mo.name }));
      back("All models", () => { view = { mode: "list" }; render(); });
      item({ sel: !pref, onclick: () => setPref(null) },
        el("span", { class: "pi-name", text: "Default" }),
        el("span", { class: "pi-sub", text: "send nothing — server decides" }));
      for (const meth of REASONING_METHODS) {
        item({
          sel: pref?.method === meth.id,
          onclick: () => { view = { mode: "level", model: mo, method: meth }; render(); },
        },
          el("span", { class: "pi-name", text: meth.name }),
          el("span", { class: "pi-sub",
            text: meth.sub + (pref?.method === meth.id ? " · " + pref.level : "") }));
      }
      return;
    }

    // level layer
    const meth = view.method;
    list.append(el("div", { class: "popup-empty",
      text: meth.name + " — " + mo.name }));
    back("Back", () => { view = { mode: "method", model: mo }; render(); });
    for (const lv of meth.levels) {
      item({
        sel: pref?.method === meth.id && pref?.level === lv,
        onclick: () => setPref({ method: meth.id, level: lv }),
      },
        el("span", { class: "pi-name", text: lv }));
    }
  }

  render();
  // live ● updates while open — but never stomp the reasoning submenu
  window._modelPopupRefresh = () => { if (view.mode === "list") render(); };
  menu._onclose = () => { window._modelPopupRefresh = null; };
}

/* ---------- the permission-mode popup ---------- */
function permMenu(anchor, chatId) {
  const { close } = popupMenu(anchor, (m) => {
    const list = el("div", { class: "popup-list" });
    const cs = chatState(chatId);
    const active = activePermMode(cs);
    const dflt = st.config?.chat?.permission_mode || "always-ask";
    const names = Object.keys(st.config?.permissionModes
      || { "always-ask": 1, "allow-edits": 1, "always-allow": 1 });
    for (const name of names) {
      list.append(el("div", {
        class: "popup-item" + (name === active ? " sel" : ""),
        onclick: async () => {
          const res = await Api.call("chat_set_mode", chatId, name);
          if (!res.ok) { toast(res.error, "err"); return; }
          chatState(chatId).chat.permMode = name;
          renderPermPill(chatId);
          refreshChatSilently(chatId);   // tool specs changed
          close();
        },
      },
        el("span", { html: icon("shield", 12) }),
        el("span", { class: "pi-name", text: name }),
        name === dflt ? el("span", { class: "pi-sub", text: "default" }) : null));
    }
    m.append(list);
  });
}

/* ---------- context usage chip + hover breakdown ---------- */
function fmtTok(n) {
  n = Number(n);
  if (!Number.isFinite(n) || n < 0) n = 0;
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(Math.round(n));
}

/* a tiny donut: how full the context is, at a glance */
function ctxRingHtml(pct) {
  const r = 5;
  const c = 2 * Math.PI * r;
  const filled = Math.max(0, Math.min(100, pct)) / 100 * c;
  return '<svg viewBox="0 0 14 14" width="13" height="13" class="ctx-ring"'
    + ' aria-hidden="true">'
    + `<circle cx="7" cy="7" r="${r}" fill="none" stroke="currentColor"`
    + ' stroke-opacity=".22" stroke-width="2.6"/>'
    + `<circle cx="7" cy="7" r="${r}" fill="none" stroke="currentColor"`
    + ` stroke-width="2.6" stroke-dasharray="${filled.toFixed(2)} ${c.toFixed(2)}"`
    + ' transform="rotate(-90 7 7)"/>'
    + "</svg>";
}

function renderCtxChip(chatId) {
  const panel = chatPanel(chatId);
  const btn = panel?.querySelector('[data-role="ctx"]');
  if (!btn) return;
  const bd = chatState(chatId).chat?.context;
  if (!bd) { btn.classList.add("hidden"); return; }
  btn.classList.remove("hidden");
  const pct = Number.isFinite(Number(bd.pct)) ? Number(bd.pct) : null;
  const used = Number(bd.usedTokens ?? bd.estTokens) || 0;
  const nctx = Number(bd.nCtx) || 0;
  // both readings at once — tokens / window (percent) — plus the ring;
  // no toggle, nothing to remember between sessions
  const label = nctx
    ? fmtTok(used) + " / " + fmtTok(nctx)
      + (pct != null ? " (" + pct.toFixed(0) + "%)" : "")
    : fmtTok(used) + " tok";
  const thr = Number(bd.threshold) || 0.8;
  btn.classList.toggle("warn", pct != null && pct >= thr * 100);
  btn.replaceChildren(el("span", { text: label }));
  if (pct != null) btn.insertAdjacentHTML("beforeend", ctxRingHtml(pct));
}

/* the one open ctx card (hover-only) — closed on tab switches */
let _ctxCardClose = null;
function closeCtxCard() {
  if (_ctxCardClose) { const f = _ctxCardClose; _ctxCardClose = null; f(); }
}

function wireCtxHover(btn, chatId) {
  let card = null, hideTimer = null;
  const destroy = () => {
    card?.remove();
    card = null;
    if (_ctxCardClose === destroy) _ctxCardClose = null;
  };
  const hide = () => {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(destroy, 250);
  };
  const cancelHide = () => clearTimeout(hideTimer);
  const show = () => {
    cancelHide();
    if (card) return;
    const bd = chatState(chatId).chat?.context;
    if (!bd) return;
    // debugging hook: the EXACT object this card renders, every open
    try { console.debug("[loom] ctx breakdown", chatId, JSON.parse(JSON.stringify(bd))); } catch (e) { /* noop */ }
    closeCtxCard();
    _ctxCardClose = destroy;
    card = el("div", { class: "popup ctx-card" });
    const row = (k, v) => {
      let s = String(v ?? "—");
      if (s === "null" || s === "undefined" || s === "NaN") s = "—";
      return el("div", { class: "ctx-row" },
        el("span", { text: k }), el("b", { text: s }));
    };
    const p = bd.parts || {};
    const pct = Number.isFinite(Number(bd.pct)) ? Number(bd.pct) : null;
    const nctx = Number(bd.nCtx) || 0;
    // NATIVE append() stringifies null into a literal "null" TEXT NODE —
    // the source of the phantom nulls. Conditional children must be
    // filtered before they ever reach append().
    card.append(...[
      row("System prompt", fmtTok(p.system)),
      bd.compacted ? row("Compacted summary", fmtTok(p.compacted)) : null,
      row("Your messages", fmtTok(p.user)),
      row("Model responses", fmtTok(p.assistant)),
      row("Thoughts (last kept)", fmtTok(p.thoughts)),
      row("Tool results", fmtTok(p.toolResults)),
      row("Tool definitions", fmtTok(p.toolSpecs)),
      bd.images ? row("Images (not estimated)", String(bd.images)) : null,
      el("div", { class: "ctx-sep2" }),
      row("Estimated total", fmtTok(bd.estTokens)),
      row("Last turn (actual)",
        bd.lastUsedTokens ? fmtTok(bd.lastUsedTokens) : "— (no turns yet)"),
      row("Context window", nctx
        ? fmtTok(nctx) + (pct != null ? ` (${pct.toFixed(1)}% used)` : "")
        : "unknown — model not in loom.yaml"),
      el("div", { class: "ctx-sep2" }),
      row("Auto-compaction", bd.auto
        ? "on at " + Math.round((Number(bd.threshold) || 0.8) * 100) + "%"
        : "off"),
    ].filter(Boolean));
    card.append(el("div", { class: "ctx-actions" },
        el("button", {
          class: "btn btn-sm", text: "Diagnostics",
          title: "Per-entry token graph + table for this chat",
          onclick: () => { destroy(); openDiagTab(chatId); },
        }),
        el("span", { class: "spacer" }),
        el("button", {
          class: "btn btn-sm", text: "Compact now",
          onclick: async () => {
            const res = await Api.call("chat_compact", chatId);
            if (!res.ok) toast(res.error, "err");
            card?.remove(); card = null;
          },
        })));
    document.body.append(card);
    const a = btn.getBoundingClientRect();
    const r = card.getBoundingClientRect();
    card.style.left = Math.max(8, Math.min(a.right - r.width,
      window.innerWidth - r.width - 8)) + "px";
    card.style.top = Math.max(8, a.top - r.height - 8) + "px";
    card.addEventListener("mouseenter", cancelHide);
    card.addEventListener("mouseleave", hide);
  };
  btn.addEventListener("mouseenter", show);
  btn.addEventListener("mouseleave", hide);
}

function renderChat(chatId) {
  const panel = chatPanel(chatId);
  const cs = chatState(chatId);
  if (!panel || !cs.chat) return;

  renderCtxChip(chatId);
  renderNetChip(chatId);
  renderModelButton(chatId);
  renderPermPill(chatId);
  renderContainerPill(chatId);
  renderEnvPill(chatId);
  renderAttachBar(chatId);
  renderChatThread(chatId);   // consumes cs.restoreScroll when visible
  renderSendButton(chatId);
}

function renderSendButton(chatId) {
  const panel = chatPanel(chatId);
  if (!panel) return;
  const cs = chatState(chatId);
  const b = panel.querySelector('[data-role="send"]');
  b.classList.toggle("stop", !!cs.running);
  // stop is a FILLED square (currentColor = black via .stop), not the
  // stroked outline the icon set draws
  b.innerHTML = cs.running
    ? '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
      + '<rect x="7" y="7" width="10" height="10" rx="1.5"'
      + ' fill="currentColor"/></svg>'
    : icon("up");
  b.title = cs.running ? "Stop generating  (Esc)" : "Send  (Enter)";
  // only the stop state earns a chip — Enter-to-send is placeholder lore
  if (cs.running) setHotkey(b, "Esc");
  else b.removeAttribute("data-hotkey");
  renderComposerHints(chatId);
}

/* the placeholder explains the keys that matter RIGHT NOW */
function renderComposerHints(chatId) {
  const panel = chatPanel(chatId);
  if (!panel) return;
  const cs = chatState(chatId);
  const waiting = Object.values(cs.tools || {}).some((t) => t.state === "waiting");
  panel._input.placeholder = waiting
    ? "A tool call is waiting — Ctrl+Enter allows it, Ctrl+Esc denies it."
    : cs.running
      ? "Generating — Esc stops it. Enter queues your next message."
      : "Ask anything…  (Enter to send · Shift+Enter for a newline · Ctrl+I focuses here)";
}

/* ---------- the churn note ----------
 * A small comfort line whenever the turn passes back to the user: how
 * long the whole response took, dressed in a randomly picked phrase.
 * The phrase is rolled ONCE per turn end (stored, so redraws don't
 * reroll it) and cleared when the next response starts. */
const CHURN_PHRASES = [
  "Churned for", "Cooked for", "Mulled it over for", "Cranked away for",
  "Ruminated for", "Wrangled tokens for", "Simmered for",
  "Chewed on that for", "Toiled for", "Spun the loom for",
];
const CHURN_CANCEL_PHRASES = [
  "Yanked the plug after", "Cut short after", "Reined in after",
  "Interrupted mid-thought after", "Called off after",
];

function mkChurn(cs, cancelled) {
  if (!cs.respT0) return null;
  const ms = performance.now() - cs.respT0;
  cs.respT0 = null;
  if (ms < 1000) return null;   // instant turns don't need a eulogy
  const pool = cancelled ? CHURN_CANCEL_PHRASES : CHURN_PHRASES;
  const phrase = pool[Math.floor(Math.random() * pool.length)];
  return { text: phrase + " " + fmtDur(Math.round(ms)) + "." };
}

/* ---------- live-stream liveness ----------
 * A token count rides after the streaming cursor and ticks up with every
 * delta (thinking included). Its heartbeat IS the health indicator: a
 * frozen number means the stream stalled — no more wondering. */
function liveTok(cs) {
  return Math.round(((cs.live?.text || "").length
    + (cs.live?.think || "").length) / 4);
}

/* before the first token the server is PREFILLING (reading the whole
 * prompt) — show ticking elapsed time so slow-to-first-token models
 * read as busy, not broken */
function liveCountText(cs) {
  const n = liveTok(cs);
  if (n > 0) return "~" + fmtTok(n) + " tok";
  const secs = cs.turnT0
    ? Math.max(0, (performance.now() - cs.turnT0) / 1000) : 0;
  return "reading prompt… " + secs.toFixed(0) + "s";
}

function liveCountEl(cs) {
  return el("span", { class: "live-count", "data-role": "live-count",
    text: liveCountText(cs),
    title: "Liveness. Before the first token the server is processing "
      + "the prompt (the elapsed time ticks); then this becomes the "
      + "tokens streamed so far, thinking included (estimated). Frozen "
      + "= stalled." });
}

function tickLiveCount(chatId) {
  const cs = chatState(chatId);
  const n = chatPanel(chatId)?.querySelector('[data-role="live-count"]');
  if (n) n.textContent = liveCountText(cs);
}

/* the prefill clock only moves if something re-renders it — a light
 * ticker keeps the active chat's zero-token phase visibly alive */
let _liveTicker = null;
function ensureLiveTicker() {
  if (_liveTicker) return;
  _liveTicker = setInterval(() => {
    const tab = tabById(st.activeTab);
    if (tab?.type !== "chat") return;
    const cs = st.chats[tab.chatId];
    if (cs?.running && liveTok(cs) === 0) tickLiveCount(tab.chatId);
  }, 500);
}

/* EAGER cancel (the stop button and Esc): the user is rejecting the
 * output — the UI stops NOW and stale events are discarded while the
 * backend unwinds. Returns false when nothing was running. */
function stopChatGeneration(chatId) {
  const cs = chatState(chatId);
  if (!cs.running) return false;
  Api.call("chat_stop", chatId);
  cs.churn = mkChurn(cs, true) || cs.churn;
  cs.discarding = true;
  cs.running = false;
  cs.live = null;
  cs.tools = {};
  renderSendButton(chatId);
  renderChatThread(chatId);
  renderTabs();
  return true;
}

/* the first tool call waiting on permission in a chat, if any */
function waitingToolId(chatId) {
  for (const [callId, t] of Object.entries(st.chats[chatId]?.tools || {})) {
    if (t.state === "waiting") return callId;
  }
  return null;
}

/* Ctrl+I from anywhere: land in a message input — the active chat's, or
 * the first open chat tab's */
function focusMessageInput() {
  let tab = tabById(st.activeTab);
  if (tab?.type !== "chat") {
    tab = st.tabs.find((t) => t.type === "chat");
    if (!tab) return;
    activateTab(tab.id);   // focuses the input itself
    return;
  }
  panelFor(tab.id)?.querySelector(".compose-input")?.focus();
}

function atBottom(thread) {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60;
}

function selectionWithin(node) {
  const s = getSelection();
  return !!(s && s.rangeCount && !s.isCollapsed && node && node.contains(s.anchorNode));
}

function renderChatThread(chatId) {
  const panel = chatPanel(chatId);
  const cs = chatState(chatId);
  if (!panel || !cs.chat) return;
  const thread = panel.querySelector('[data-role="thread"]');
  // copying from the thread must survive streaming: never rebuild the DOM
  // out from under a live selection — retry once it's gone
  if (selectionWithin(thread)) {
    clearTimeout(cs._selRetry);
    cs._selRetry = setTimeout(() => renderChatThread(chatId), 1000);
    return;
  }
  const stick = atBottom(thread);
  thread.replaceChildren();

  const msgs = cs.chat.messages || [];
  // windowed history: only the fetched tail is in msgs; older messages
  // load on demand (keeps 100k-message chats usable — render and bridge
  // transfer stay O(window))
  const hidden = Math.max(0, (cs.chat.totalMessages ?? msgs.length) - msgs.length);
  if (hidden > 0) {
    thread.append(el("div", { class: "chat-resume" },
      el("button", {
        class: "btn btn-sm",
        text: `↑ Show earlier messages (${hidden.toLocaleString()} hidden)`,
        onclick: () => loadEarlierMessages(chatId),
      })));
  }
  // a turn's stats render AFTER everything the turn produced: a turn
  // that called tools holds its stats row back until the last of its
  // tool RESULTS is on screen
  let pendingStats = null;   // {row, calls: Set of outstanding call ids}
  const flushStats = () => {
    if (pendingStats) {
      thread.append(pendingStats.row);
      pendingStats = null;
    }
  };
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (m.role === "user") {
      flushStats();
      thread.append(userMsgEl(m));
    } else if (m.role === "assistant") {
      flushStats();
      // the THOUGHT is its own entry above the message, carrying the
      // model's name and its own size when expanded
      if (m.thinking) thread.append(thinkEntryEl(chatId, m, cs.chat.model, i));
      const node = assistantMsgEl(chatId, m, cs.chat.model, i);
      if (node) thread.append(node);
      if (m.timings || m.usage) {
        const row = statsRow(m);
        const calls = new Set((m.tool_calls || []).map((c) => c.id));
        if (calls.size) {
          // this row will land under tool cards — align it with THEIR
          // centered, narrower column instead of the message nudge
          row.classList.add("after-tools");
          pendingStats = { row, calls };
        } else {
          thread.append(row);
        }
      }
    } else if (m.role === "tool") {
      thread.append(toolCardEl(chatId, {
        callId: m.tool_call_id, tool: m.name,
        args: argsOfCall(msgs, i, m.tool_call_id),
        state: m.ok === false ? "failed" : "done",
        result: m.content,
      }));
      if (pendingStats) {
        pendingStats.calls.delete(m.tool_call_id);
        if (!pendingStats.calls.size) flushStats();
      }
    } else if (m.role === "compact") {
      flushStats();
      thread.append(compactCardEl(chatId, m, i));
    }
  }
  if (cs.compacting) {
    thread.append(el("div", { class: "sysnote", "data-role": "compacting",
      text: "Compacting context… ~" + fmtTok(cs.compactTok || 0)
        + " tok summarized" }));
  }

  // live streaming block — the cursor FOLLOWS the phases, in order:
  //   prefill  → a bare cursor line (no header yet; elapsed time ticks)
  //   thinking → the thought entry, EXPANDED, cursor inside it
  //   message  → thought collapses, the model-name header appears, and
  //              the cursor moves into the streaming message
  if (cs.live) {
    const name = cs.chat.model || "assistant";
    const inMessage = !!cs.live.text;
    const inThink = !!cs.live.think && !inMessage;
    if (!cs.live.think && !cs.live.text) {
      // prefill: nothing exists yet — just the heartbeat
      thread.append(el("div", { class: "msg live-wait" },
        el("span", { class: "cursor" }), liveCountEl(cs)));
    }
    if (cs.live.think) {
      const t = el("div", {
        class: "think" + (inThink ? "" : " collapsed"),
        "data-role": "live-think",
      });
      // text NODE first: the fast path updates firstChild.nodeValue so
      // the cursor/count siblings survive every delta
      t.append(document.createTextNode(cs.live.think));
      if (inThink) t.append(el("span", { class: "cursor" }), liveCountEl(cs));
      thread.append(el("div", { class: "msg think-entry" },
        el("div", { class: "think-toggle",
          text: inThink ? "▾ " + name + " — thinking…"
                        : "▸ " + name + " thought for a bit" }),
        t));
    }
    if (inMessage) {
      const wrap = el("div", { class: "msg assistant" });
      wrap.append(el("div", { class: "msg-head" },
        el("span", { class: "who", text: name })));
      const body = el("div", { class: "msg-body md-render", "data-role": "live" });
      body.innerHTML = renderMarkdown(cs.live.text || "");
      if ((cs.live.text || "").length <= ENHANCE_LIVE_MAX) enhanceCodeBlocks(body);
      body.append(el("span", { class: "cursor" }), liveCountEl(cs));
      wrap.append(body);
      thread.append(wrap);
    }
  }
  // waiting permission cards render inline via tool state (already in tools)
  for (const [callId, t] of Object.entries(cs.tools)) {
    if (t.state === "waiting" || t.state === "running") {
      thread.append(toolCardEl(chatId, { callId, ...t }));
    }
  }
  // a turn whose tool calls are still executing: its stats land after
  // the live cards — never before the results they belong with
  flushStats();
  if (cs.error) {
    thread.append(el("div", { class: "sysnote err", text: cs.error }));
  }
  // the churn note: the turn is back in the user's hands
  if (cs.churn && !cs.running && !cs.live) {
    thread.append(el("div", { class: "churn-note", text: cs.churn.text }));
  }
  // resume affordance: last word was the user's (error/deny ended the
  // turn) → Retry; the model stopped abruptly (Stop / broken stream) →
  // Continue. Both re-run the loop on the chat as it stands.
  if (!cs.running && !cs.live && msgs.length) {
    const last = msgs[msgs.length - 1];
    let label = null;
    if (last.role === "user" || last.role === "tool") {
      label = "↻ Retry — generate a response";
    } else if (last.role === "assistant" && last.stopped) {
      label = "→ Continue — the response stopped early";
    }
    if (label) {
      thread.append(el("div", { class: "chat-resume" },
        setHotkey(el("button", {
          class: "btn btn-sm", "data-role": "resume", text: label,
          title: label.startsWith("↻")
            ? "Generate a response to the last message  (Ctrl+R)"
            : "Continue the response that stopped early  (Ctrl+R)",
          onclick: () => continueChat(chatId),
        }), "Ctrl+R")));
    }
  }
  if (!msgs.length && !cs.live) {
    thread.append(el("div", { class: "empty-hint" },
      el("div", { class: "big", text: "New chat" }),
      el("div", { text: "Attach images or folders below; shell commands run sandboxed in a container." })));
  }
  if (stick) thread.scrollTop = thread.scrollHeight;
  // an explicit position (tab switch, session restore) beats sticking —
  // the user was comparing chats mid-scroll; put them back exactly there.
  // Only consumable while visible: a hidden panel can't scroll.
  if (cs.restoreScroll != null && thread.clientHeight) {
    // CSSOM trap: assigning Infinity to scrollTop is normalized to 0 —
    // the "restore to bottom" sentinel must become a real pixel value
    thread.scrollTop = Number.isFinite(cs.restoreScroll)
      ? cs.restoreScroll : thread.scrollHeight;
    cs.scrollPos = thread.scrollTop;       // read back the clamped value
    cs.restoreScroll = null;
  }
  if (panel._updateJump) panel._updateJump();
  renderComposerHints(chatId);   // tool_wait arrives via thread redraws
}

async function loadEarlierMessages(chatId) {
  const cs = chatState(chatId);
  cs.windowSize += 500;
  const panel = chatPanel(chatId);
  const thread = panel?.querySelector('[data-role="thread"]');
  const oldH = thread ? thread.scrollHeight : 0;
  const oldTop = thread ? thread.scrollTop : 0;
  const res = await Api.call("chat_get", chatId, cs.windowSize);
  if (!res.ok) { toast(res.error, "err"); return; }
  cs.chat = res.data.chat;
  cs.running = res.data.running;
  renderChatThread(chatId);
  // keep the message the user was looking at where it was
  if (thread) thread.scrollTop = thread.scrollHeight - oldH + oldTop;
}

function argsOfCall(msgs, uptoIdx, callId) {
  for (let i = uptoIdx; i >= 0; i--) {
    const m = msgs[i];
    if (m.role === "assistant" && m.tool_calls) {
      const c = m.tool_calls.find((x) => x.id === callId);
      if (c) return c.function?.arguments;
    }
  }
  return "";
}

/* ---------- image previews (thumbnails + hover zoom) ---------- */
function fileUrl(p) {
  return "file://" + encodeURI(String(p)).replace(/#/g, "%23").replace(/\?/g, "%3F");
}

let _imgPrev = null;
function wireImgPreview(elm, path) {
  const hide = () => { _imgPrev?.remove(); _imgPrev = null; };
  elm.addEventListener("mouseenter", () => {
    hide();
    const img = el("img", { src: fileUrl(path) });
    _imgPrev = el("div", { class: "img-preview" }, img,
      el("div", { class: "img-preview-name", text: baseName(path) }));
    document.body.append(_imgPrev);
    const place = () => {
      if (!_imgPrev) return;
      const a = elm.getBoundingClientRect();
      const r = _imgPrev.getBoundingClientRect();
      const left = Math.max(8, Math.min(a.left, window.innerWidth - r.width - 8));
      let top = a.top - r.height - 8;
      if (top < 8) top = Math.min(a.bottom + 8, window.innerHeight - r.height - 8);
      _imgPrev.style.left = left + "px";
      _imgPrev.style.top = Math.max(8, top) + "px";
    };
    place();
    img.addEventListener("load", place);   // re-clamp once dimensions exist
    img.addEventListener("error", hide);
  });
  elm.addEventListener("mouseleave", hide);
}

function imgThumb(path) {
  const img = el("img", {
    class: "msg-thumb", src: fileUrl(path),
    alt: baseName(path), title: baseName(path),
  });
  img.addEventListener("error", () => {
    img.replaceWith(el("span", { class: "pill", text: "🖼 " + baseName(path) }));
  });
  wireImgPreview(img, path);
  return img;
}

function userMsgEl(m) {
  const wrap = el("div", { class: "msg user" });
  wrap.append(el("div", { class: "msg-head" },
    el("span", { class: "who", text: "you" }),
    tago(m.ts)));
  const body = el("div", { class: "msg-body", text: m.content || "" });
  wrap.append(body);
  if (m.images?.length) {
    wrap.append(el("div", { class: "msg-imgs" },
      ...m.images.map((im) => imgThumb(im.path))));
  }
  return wrap;
}

/* the thought as a standalone entry: model name in its header, its own
 * estimated stats revealed with the text. Expanded/collapsed survives
 * redraws AND tab switches — tracked in chat state like tool cards. */
function thinkEntryEl(chatId, m, model, idx) {
  const openMap = chatState(chatId).thinkOpen
    || (chatState(chatId).thinkOpen = {});
  const key = "think:" + (m.ts || idx);
  const isOpen = !!openMap[key];
  const name = model || "assistant";
  const label = (open) => (open ? "▾ " : "▸ ") + name
    + (open ? " — thinking" : " thought for a bit");
  const wrap = el("div", { class: "msg think-entry" });
  const think = el("div", {
    class: "think" + (isOpen ? "" : " collapsed"), text: m.thinking,
  });
  // the thought's own size is the one thing that IS splittable; the
  // turn's real stats render after everything the turn produced
  const tokens = Math.round((m.thinking || "").length / 4);
  const stats = el("div", {
    class: "think-stats" + (isOpen ? "" : " hidden"),
    text: "~" + fmtTok(tokens) + " tok (estimated)",
    title: "Estimated size of the thought (characters ÷ 4). The server "
      + "reports speed/time per TURN, not per part — those live in the "
      + "stats row at the end of the turn.",
  });
  const tog = el("div", {
    class: "think-toggle", text: label(isOpen),
    onclick: () => {
      const nowOpen = !think.classList.toggle("collapsed");
      if (nowOpen) openMap[key] = true;
      else delete openMap[key];
      tog.textContent = label(nowOpen);
      stats.classList.toggle("hidden", !nowOpen);
    },
  });
  wrap.append(tog, think, stats);
  return wrap;
}

function assistantMsgEl(chatId, m, model, idx) {
  if (!m.content) return null;  // pure tool turn / thinking-only: no body
  const wrap = el("div", { class: "msg assistant" });
  wrap.append(el("div", { class: "msg-head" },
    el("span", { class: "who", text: model || "assistant" }),
    tago(m.ts)));
  if (m.content) {
    const body = el("div", { class: "msg-body md-render" });
    body.innerHTML = renderMarkdown(m.content);
    enhanceCodeBlocks(body);
    wrap.append(body);
  }
  return wrap;
}

/* ---------- code blocks in rendered markdown ----------
 * marked emits <pre><code class="language-x">; decorate each block with
 * the editor's tokenizer (same colors as everywhere else) and a hover
 * Copy button. Skipped for very large live streams (O(n²) re-highlight
 * per delta) — those get their final pass when the message persists. */
const ENHANCE_LIVE_MAX = 20000;

function enhanceCodeBlocks(root) {
  for (const pre of root.querySelectorAll("pre")) {
    const code = pre.querySelector("code");
    if (!code) continue;
    const m = /language-(\S+)/.exec(code.className || "");
    const lang = edLangFor(m ? m[1] : "");
    if (lang) {
      const text = code.textContent;
      const frag = document.createDocumentFragment();
      let inBlock = false;
      const lines = text.replace(/\n$/, "").split("\n");
      lines.forEach((line, i) => {
        const out = edHighlight(line, lang, inBlock);
        inBlock = out.inBlock;
        for (const [t, c] of out.segs) {
          if (!t) continue;
          if (c) {
            const sp = document.createElement("span");
            sp.className = c;
            sp.textContent = t;
            frag.append(sp);
          } else {
            frag.append(document.createTextNode(t));
          }
        }
        if (i < lines.length - 1) frag.append(document.createTextNode("\n"));
      });
      code.replaceChildren(frag);
    }
    const btn = el("button", { class: "code-copy", text: "Copy", title: "Copy this code block" });
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const ok = await copyText(code.textContent);
      btn.textContent = ok ? "✓ copied" : "copy failed";
      setTimeout(() => { btn.textContent = "Copy"; }, 1400);
    });
    pre.append(btn);
  }
}

/* ---------- code / diff previews for file tools ---------- */
function previewLang(path) {
  const conf = editorModeFor(baseName(String(path || "")));
  return conf.mode === "code" ? conf.lang : "";
}

function _codeLineEl(line, lang, state, gutter, cls) {
  const out = edHighlight(line, lang, state.inBlock);
  state.inBlock = out.inBlock;
  const lc = el("span", { class: "lc" });
  for (const [txt, c] of out.segs) {
    if (!txt) continue;
    if (c) lc.append(el("span", { class: c, text: txt }));
    else lc.append(document.createTextNode(txt));
  }
  return el("div", { class: "cl" + (cls ? " " + cls : "") },
    el("span", { class: "ln", text: gutter }), lc);
}

// generous: tool results are backend-capped anyway, and previews only
// render at full length once the user expands the card to inspect it
const PREV_MAX_LINES = 1500;

/* read previews: numbered, syntax-highlighted */
function codePreviewEl(text, lang, startLine) {
  const wrap = el("div", { class: "code-prev" });
  const lines = String(text ?? "").replace(/\n$/, "").split("\n");
  const state = { inBlock: false };
  lines.slice(0, PREV_MAX_LINES).forEach((line, i) => {
    wrap.append(_codeLineEl(line, lang, state, String((startLine || 1) + i)));
  });
  if (lines.length > PREV_MAX_LINES) {
    wrap.append(el("div", { class: "cl more" },
      el("span", { class: "ln", text: "" }),
      el("span", { class: "lc", text: "… " + (lines.length - PREV_MAX_LINES) + " more lines" })));
  }
  return wrap;
}

/* write previews: a −old/+new syntax-highlighted diff */
function diffPreviewEl(oldText, newText, lang) {
  const wrap = el("div", { class: "code-prev" });
  const block = (text, kind, mark) => {
    if (text === null || text === undefined || text === "") return;
    const lines = String(text).replace(/\n$/, "").split("\n");
    const state = { inBlock: false };
    for (const line of lines.slice(0, PREV_MAX_LINES)) {
      wrap.append(_codeLineEl(line, lang, state, mark, kind));
    }
    if (lines.length > PREV_MAX_LINES) {
      wrap.append(el("div", { class: "cl more " + kind },
        el("span", { class: "ln", text: mark }),
        el("span", { class: "lc", text: "… " + (lines.length - PREV_MAX_LINES) + " more lines" })));
    }
  };
  block(oldText, "del", "−");
  block(newText, "add", "+");
  return wrap;
}

function toolArgsObj(t) {
  if (t.args && typeof t.args === "object") return t.args;
  if (typeof t.args === "string") {
    try {
      const o = JSON.parse(t.args);
      if (o && typeof o === "object") return o;
    } catch (e) { /* raw string */ }
  }
  return {};
}

function toolCardEl(chatId, t) {
  const stateTxt = {
    announced: "…", waiting: "waiting for permission", running: "running…",
    done: "ok", failed: "failed", denied: "denied",
  }[t.state] || t.state;
  const a = toolArgsObj(t);
  const isFileTool = ["read_file", "edit_file", "write_file"].includes(t.tool);
  // resolved cards collapse to a one-line preview; clicking toggles.
  // Active cards (waiting/running) always render in full.
  const resolved = ["done", "failed", "denied"].includes(t.state);
  const open = !resolved || !!chatState(chatId).toolsOpen[t.callId];
  const summary = String(
    a.path || a.query || a.command || "").split("\n")[0].slice(0, 120);
  const card = el("div", {
    class: "tool-card " + (open ? "open" : "closed") + (resolved ? " resolved" : ""),
  });
  const head = el("div", { class: "tool-head" + (resolved ? " toggleable" : "") },
    resolved ? el("span", { class: "tool-chevron", text: open ? "▾" : "▸" }) : null,
    el("span", { html: icon("gear", 13) }),
    el("span", { class: "tool-name", text: t.tool || "tool" }),
    el("span", {
      class: "tool-state" + (t.state === "failed" || t.state === "denied" ? " err" : t.state === "done" ? " ok" : ""),
      text: stateTxt,
    }));
  if (summary) head.append(el("span", { class: "tool-path", text: summary, title: summary }));
  if (resolved) {
    head.addEventListener("click", () => {
      const openMap = chatState(chatId).toolsOpen;
      if (openMap[t.callId]) delete openMap[t.callId];
      else openMap[t.callId] = true;
      renderChatThread(chatId);
    });
  }
  card.append(head);
  if (!open) return card;   // the minimal preview row is the whole card

  // file tools show previews, not raw JSON args
  if (!isFileTool) {
    const args = typeof t.args === "string" ? t.args : JSON.stringify(t.args ?? {}, null, 1);
    if (args && args !== "{}") {
      card.append(el("div", { class: "tool-args", text: args }));
    }
  }
  if (t.tool === "edit_file" && (a.old_string || a.new_string)) {
    card.append(diffPreviewEl(a.old_string, a.new_string, previewLang(a.path)));
  } else if (t.tool === "write_file" && a.content) {
    card.append(diffPreviewEl(null, a.content, previewLang(a.path)));
  }

  if (t.state === "waiting") {
    card.append(el("div", { class: "tool-perm" },
      el("span", { class: "q", text: "Allow this tool call?" }),
      setHotkey(el("button", { class: "btn btn-sm", text: "Deny", title: "Deny  (Ctrl+Esc)", onclick: () => Api.call("tool_answer", t.callId, "deny") }), "Ctrl+Esc"),
      setHotkey(el("button", { class: "btn btn-sm btn-acc", text: "Allow", title: "Allow  (Ctrl+Enter)", onclick: () => Api.call("tool_answer", t.callId, "allow") }), "Ctrl+Enter")));
    return card;
  }
  if (t.tool === "read_file" && t.state === "done" && t.result) {
    // sliced reads carry "[lines A-B of T]" as their first line
    let body = String(t.result), start = 1;
    const m = /^\[lines (\d+)-\d+ of \d+\]\n?/.exec(body);
    if (m) { start = +m[1]; body = body.slice(m[0].length); }
    card.append(codePreviewEl(body, previewLang(a.path), start));
  } else if (isFileTool) {
    if (t.result) {
      card.append(el("div", { class: "tool-note", text: String(t.result).slice(0, 500) }));
    }
  } else {
    const out = t.output || t.result;
    if (out) {
      card.append(el("div", { class: "tool-out", text: String(out).slice(0, 8000) }));
    }
  }
  return card;
}

/* compaction marker: everything above it left the model's context — the
 * collapsed row says so; expanding shows the summary that replaced it */
function compactCardEl(chatId, m, idx) {
  const key = "compact:" + (m.ts || idx);
  const openMap = chatState(chatId).toolsOpen;
  const open = !!openMap[key];
  const card = el("div", {
    class: "tool-card resolved " + (open ? "open" : "closed"),
  });
  const head = el("div", { class: "tool-head toggleable" },
    el("span", { class: "tool-chevron", text: open ? "▾" : "▸" }),
    el("span", { html: icon("archive", 13) }),
    el("span", { class: "tool-name", text: "context compacted" }),
    el("span", {
      class: "tool-state",
      text: (m.replaced || 0) + " earlier messages summarized",
    }));
  head.addEventListener("click", () => {
    if (openMap[key]) delete openMap[key];
    else openMap[key] = true;
    renderChatThread(chatId);
  });
  card.append(head);
  if (open) {
    const body = el("div", { class: "msg-body md-render" });
    body.innerHTML = renderMarkdown(m.content || "");
    enhanceCodeBlocks(body);
    card.append(body);
  }
  return card;
}

function statsRow(m) {
  const row = el("div", { class: "msg-stats" });
  const t = m.timings || {};
  const u = m.usage || {};
  const stat = (k, v, tip) => el("span", { class: "stat", title: tip },
    el("b", { text: v }), " " + k);
  const totalMs = (t.prompt_ms || 0) + (t.predicted_ms || 0);
  if (totalMs > 0) {
    row.append(stat("took", fmtDur(Math.round(totalMs)),
      "Total turn time — prompt processing ("
      + fmtDur(Math.round(t.prompt_ms || 0)) + ") plus generation ("
      + fmtDur(Math.round(t.predicted_ms || 0)) + ")"));
  }
  if (t.predicted_per_second) {
    row.append(stat("tok/s", t.predicted_per_second.toFixed(1),
      "Generation speed — output tokens per second for this turn"));
  }
  if (m.ttftMs) {
    row.append(stat("ttft", (m.ttftMs / 1000).toFixed(2) + "s",
      "Time to first token — how long the server processed the prompt "
      + "before anything streamed back"));
  }
  if (u.prompt_tokens != null) {
    row.append(stat("prompt", u.prompt_tokens,
      "Prompt tokens — the full context the model read this turn "
      + "(system prompt, history, tool results, tool definitions)"));
  }
  const outTip = "Output tokens — everything the model generated this "
    + "turn: thinking, the message, and any tool calls";
  if (u.completion_tokens != null) row.append(stat("out", u.completion_tokens, outTip));
  else if (t.predicted_n != null) row.append(stat("out", t.predicted_n, outTip));
  return row;
}

/* ---------- sending: the visible queue ----------
 * A sent message leaves the input immediately and lands in the queue
 * panel above the input. It dispatches when the model is running and the
 * chat is idle; each item can be edited back into the input, force-sent,
 * or cancelled. */

function renderQueue(chatId) {
  const panel = chatPanel(chatId);
  const host = panel?.querySelector('[data-role="queue"]');
  if (!host) return;
  const cs = chatState(chatId);
  // the queue panel grows/shrinks the composer, which resizes the thread
  // viewport — a bottom-follower must be re-stuck or the growth HIDES
  // the newest generated text behind the composer
  const thread = panel.querySelector('[data-role="thread"]');
  const wasBottom = thread && thread.clientHeight && atBottom(thread);
  host.replaceChildren();
  (cs.queue || []).forEach((q, i) => {
    host.append(el("div", { class: "queue-row" },
      el("span", { class: "q-badge", text: "queued" }),
      el("span", {
        class: "q-text", title: q.text,
        text: (q.text || "(images only)").slice(0, 300),
      }),
      q.images?.length ? el("span", { class: "q-imgs", text: q.images.length + " 🖼" }) : null,
      el("button", {
        class: "btn btn-sm", text: "Edit",
        title: "Return this message to the input for editing",
        onclick: () => queueToInput(chatId, i),
      }),
      el("button", {
        class: "btn btn-sm", text: "Send now",
        title: "Move to the front and send immediately",
        onclick: () => {
          const cs2 = chatState(chatId);
          const [it] = cs2.queue.splice(i, 1);
          cs2.queue.unshift(it);
          renderQueue(chatId);
          attemptFlush(chatId, true);
        },
      }),
      el("button", {
        class: "btn btn-sm", text: "×", title: "Cancel this message",
        onclick: () => { cs.queue.splice(i, 1); renderQueue(chatId); },
      })));
  });
  // re-stick after the composer resized (layout settles this frame)
  if (wasBottom) {
    requestAnimationFrame(() => {
      thread.scrollTop = thread.scrollHeight;
      panel._updateJump?.();
    });
  }
}

function queueToInput(chatId, i) {
  const panel = chatPanel(chatId);
  const cs = chatState(chatId);
  const [it] = cs.queue.splice(i, 1);
  renderQueue(chatId);
  if (!panel || !it) return;
  const input = panel._input;
  // whatever was being typed is preserved after the returned message
  input.value = it.text + (input.value ? "\n\n" + input.value : "");
  cs.pendingImages = (it.images || []).concat(cs.pendingImages || []);
  renderAttachBar(chatId);
  panel._resizeInput?.();
  input.focus();
  input.setSelectionRange(it.text.length, it.text.length);
}

async function dispatchMessage(chatId, item) {
  const cs = chatState(chatId);
  cs._dispatching = true;
  cs.error = null;
  const res = await Api.call("chat_send", chatId, item.text, item.images || []);
  cs._dispatching = false;
  if (!res.ok) {
    cs.queue.unshift(item);   // nothing is ever lost — back to the head
    renderQueue(chatId);
    // losing a race with a still-unwinding stream is not an error — and
    // the done event may ALREADY have fired, so never depend on it:
    // retry shortly until the worker is really gone
    if (/already streaming/i.test(res.error || "")) {
      setTimeout(() => attemptFlush(chatId, false), 800);
    } else {
      toast(res.error, "err");
    }
    return;
  }
  cs.chat.messages.push(res.data.message);
  cs.running = true;
  cs.respT0 = performance.now();
  cs.churn = null;
  cs.turnT0 = performance.now();
  cs.live = { text: "", think: "" };
  cs.tools = {};
  renderChatThread(chatId);
  renderSendButton(chatId);
  renderTabs();
}

/* dispatch the queue head if possible; otherwise arrange for it (start
 * dialog for a stopped model; loading/streaming flush on their events) */
function attemptFlush(chatId, interactive) {
  const cs = chatState(chatId);
  if (!cs.queue?.length || cs.running || cs._dispatching) return;
  const name = chatModelName(cs);
  if (!name) {
    if (interactive) toast("No models defined in loom.yaml yet.", "warn");
    return;
  }
  const state = modelStateByName(name);
  if (state && state !== "running") {
    if (state === "starting" || state === "loading") return;  // flush on ready
    const m = (st.config?.models || []).find((x) => x.name === name);
    confirmModal("Model not running",
      name + " isn't running. Start the server? Queued messages send as "
      + "soon as the model is ready.",
      "Start model", () => {
        Api.call("server_start", m.id);
        toast("Starting " + name + "…", "ok");
        renderModelButton(chatId);
      }, false, "model-start:" + chatId);   // singleton — Enter spam can't stack it
    return;
  }
  const item = cs.queue.shift();
  renderQueue(chatId);
  dispatchMessage(chatId, item);
}

/* continue/retry still needs the start gate (no queue item involved) */
function ensureModelRunning(chatId, flag) {
  const cs = chatState(chatId);
  const name = chatModelName(cs);
  if (!name) { toast("No models defined in loom.yaml yet.", "warn"); return false; }
  const state = modelStateByName(name);
  if (!state || state === "running") return true;
  if (state === "starting" || state === "loading") {
    cs[flag] = true;
    toast(name + " is still loading — this goes as soon as it's ready.", "warn");
    return false;
  }
  const m = (st.config?.models || []).find((x) => x.name === name);
  confirmModal("Model not running",
    name + " isn't running. Start the server now? The generation resumes "
    + "as soon as the model is ready.",
    "Start model", () => {
      cs[flag] = true;
      Api.call("server_start", m.id);
      toast("Starting " + name + "…", "ok");
      renderModelButton(chatId);
    }, false, "model-start:" + chatId);
  return false;
}

/* called from the srv event stream: fire anything waiting on a start */
function chatsOnSrvEvent() {
  for (const tab of st.tabs) {
    if (tab.type !== "chat") continue;
    const cs = st.chats[tab.chatId];
    if (!cs) continue;
    const state = modelStateByName(chatModelName(cs));
    if (state === "running") {
      if (cs.autoContinue) {
        cs.autoContinue = false;
        continueChat(tab.chatId);
      } else {
        attemptFlush(tab.chatId, false);
      }
    } else if (state === "error" && (cs.queue?.length || cs.autoContinue)) {
      cs.autoContinue = false;
      toast(chatModelName(cs) + " failed to start — your queued message is "
        + "kept. See the Servers tab log.", "err");
    }
  }
}

async function continueChat(chatId) {
  const cs = chatState(chatId);
  if (cs.running) return;
  if (!ensureModelRunning(chatId, "autoContinue")) return;
  cs.error = null;
  const res = await Api.call("chat_continue", chatId);
  if (!res.ok) { toast(res.error, "err"); return; }
  cs.running = true;
  cs.respT0 = performance.now();
  cs.churn = null;
  cs.turnT0 = performance.now();
  cs.live = { text: "", think: "" };
  cs.tools = {};
  renderChatThread(chatId);
  renderSendButton(chatId);
  renderTabs();
}

function sendChatMessage(chatId) {
  const panel = chatPanel(chatId);
  const cs = chatState(chatId);
  if (!panel) return;
  const input = panel._input;
  const text = input.value.trim();
  const images = (cs.pendingImages || []).slice();
  if (!text && !images.length) return;
  // the message leaves the input IMMEDIATELY and enters the visible queue
  input.value = "";
  panel._resizeInput?.();
  cs.pendingImages = [];
  cs.histIdx = null;
  cs.queue = cs.queue || [];
  cs.queue.push({ text, images });
  renderAttachBar(chatId);
  renderQueue(chatId);
  attemptFlush(chatId, true);
}

/* ---------- input history (Up/Down at the start of the input) ---------- */
function inputHistoryList(cs) {
  const sent = (cs.chat?.messages || [])
    .filter((m) => m.role === "user" && String(m.content || "").trim())
    .map((m) => m.content);
  return sent.concat((cs.queue || []).map((q) => q.text).filter(Boolean));
}

function handleInputHistory(chatId, input, dir) {
  const cs = chatState(chatId);
  const hist = inputHistoryList(cs);
  if (!hist.length) return false;
  const panel = chatPanel(chatId);
  if (cs.histIdx == null) {
    // engage only from the very start of an input (or an empty one) —
    // otherwise the arrows move the caret like any textarea
    if (dir > 0) return false;
    if (input.value && (input.selectionStart !== 0 || input.selectionEnd !== 0)) return false;
    cs.histDraft = input.value;
    cs.histIdx = hist.length - 1;
  } else {
    const next = cs.histIdx + dir;
    if (next >= hist.length) {          // past the newest → restore the draft
      input.value = cs.histDraft || "";
      cs.histIdx = null;
      panel?._resizeInput?.();
      input.setSelectionRange(input.value.length, input.value.length);
      return true;
    }
    if (next < 0) return true;          // already at the oldest
    cs.histIdx = next;
  }
  input.value = hist[cs.histIdx];
  panel?._resizeInput?.();
  input.setSelectionRange(input.value.length, input.value.length);
  return true;
}

/* ---------- events from the Python loop ---------- */
let _chatRedrawQueued = {};
function queueThreadRedraw(chatId) {
  if (_chatRedrawQueued[chatId]) return;
  _chatRedrawQueued[chatId] = true;
  requestAnimationFrame(() => {
    _chatRedrawQueued[chatId] = false;
    renderChatThread(chatId);
  });
}

function onChatEvent(ev) {
  const chatId = ev.chatId;
  const cs = chatState(chatId);
  // after an eager cancel everything in-flight is slop being discarded —
  // only the terminal events (done/error) end the discard window
  if (cs.discarding && !["done", "error", "title",
                         "compact_done", "compact_error"].includes(ev.kind)) {
    return;
  }
  switch (ev.kind) {
    case "start":
      cs.running = true;
      cs.turnT0 = performance.now();   // the prefill clock
      cs.live = cs.live || { text: "", think: "" };
      renderSendButton(chatId);
      renderTabs();
      break;
    case "delta": {
      cs.live = cs.live || { text: "", think: "" };
      cs.live.text += ev.text;
      // fast path: patch the live body in place when it's on screen
      const panel = chatPanel(chatId);
      const liveEl = panel?.querySelector('[data-role="live"]');
      if (liveEl && !selectionWithin(liveEl)) {
        const thread = panel.querySelector('[data-role="thread"]');
        const stick = atBottom(thread);
        liveEl.innerHTML = renderMarkdown(cs.live.text);
        if (cs.live.text.length <= ENHANCE_LIVE_MAX) enhanceCodeBlocks(liveEl);
        liveEl.append(el("span", { class: "cursor" }), liveCountEl(cs));
        if (stick) thread.scrollTop = thread.scrollHeight;
        if (panel._updateJump) panel._updateJump();
      } else if (!liveEl) {
        queueThreadRedraw(chatId);
      }
      // a selection inside the live element: text keeps accumulating in
      // cs.live and lands on the next unobstructed patch
      break;
    }
    case "think": {
      cs.live = cs.live || { text: "", think: "" };
      cs.live.think += ev.text;
      // fast path mirrors delta: patch the live think block in place —
      // full-thread redraws during streaming caused visible churn
      const panel = chatPanel(chatId);
      const tEl = panel?.querySelector('[data-role="live-think"]');
      if (tEl && tEl.firstChild && !selectionWithin(tEl)) {
        const thread = panel.querySelector('[data-role="thread"]');
        const stick = atBottom(thread);
        // update the TEXT NODE only — the cursor + count siblings live
        // inside the thought while it streams and must survive deltas
        tEl.firstChild.nodeValue = cs.live.think;
        tickLiveCount(chatId);   // thinking counts toward liveness too
        if (stick) thread.scrollTop = thread.scrollHeight;
        if (panel._updateJump) panel._updateJump();
      } else if (!tEl) {
        queueThreadRedraw(chatId);
      }
      break;
    }
    case "tool_begin":
      cs.tools[ev.callId] = { tool: ev.tool, state: "announced" };
      queueThreadRedraw(chatId);
      break;
    case "tool_call":
      cs.tools[ev.callId] = {
        tool: ev.tool, args: ev.args, perm: ev.perm, state: "announced",
      };
      // the assistant turn (with its tool_calls) was just committed
      cs.live = null;
      refreshChatSilently(chatId);
      break;
    case "tool_wait":
      if (cs.tools[ev.callId]) cs.tools[ev.callId].state = "waiting";
      queueThreadRedraw(chatId);
      break;
    case "tool_exec":
      if (cs.tools[ev.callId]) cs.tools[ev.callId].state = "running";
      queueThreadRedraw(chatId);
      break;
    case "tool_output": {
      const t = cs.tools[ev.callId];
      if (t) t.output = (t.output || "") + ev.text;
      queueThreadRedraw(chatId);
      break;
    }
    case "tool_result":
      delete cs.tools[ev.callId];
      cs.live = { text: "", think: "" };   // next turn streams next
      cs.turnT0 = performance.now();       // its prefill clock restarts
      refreshChatSilently(chatId);
      break;
    case "stats": {
      // attach to the last assistant message once refreshed
      cs.lastStats = { timings: ev.timings, usage: ev.usage, ttftMs: ev.ttftMs };
      break;
    }
    case "title":
      if (cs.chat) cs.chat.title = ev.title;
      renderTabs();
      refreshArchiveTab();
      break;
    case "artifacts":
      // the model left something in /artifacts — surface it immediately
      if (cs.chat) cs.chat.artifacts = ev.items || [];
      renderAttachBar(chatId);
      if (ev.fresh?.length) {
        toast("Artifact" + (ev.fresh.length > 1 ? "s" : "") + " from the "
          + "model: " + ev.fresh.join(", "), "ok");
      }
      break;
    case "compact_start":
      cs.compacting = true;
      cs.compactTok = 0;
      queueThreadRedraw(chatId);
      break;
    case "compact_tick": {
      // the rising number IS the health indicator: frozen = stalled
      cs.compacting = true;
      cs.compactTok = ev.tokens || 0;
      const note = chatPanel(chatId)?.querySelector('[data-role="compacting"]');
      if (note) {
        note.textContent = "Compacting context… ~"
          + fmtTok(cs.compactTok) + " tok summarized";
      } else {
        queueThreadRedraw(chatId);
      }
      break;
    }
    case "compact_done":
      cs.compacting = false;
      cs.compactTok = 0;
      toast("Context compacted — " + (ev.replaced || 0)
        + " earlier messages summarized.", "ok");
      refreshChatSilently(chatId);
      break;
    case "compact_error":
      cs.compacting = false;
      cs.compactTok = 0;
      toast(ev.msg || "compaction failed", "err");
      queueThreadRedraw(chatId);
      break;
    case "done":
      cs.churn = mkChurn(cs, false) || cs.churn;
      cs.discarding = false;
      cs.running = false;
      cs.live = null;
      cs.tools = {};
      refreshChatSilently(chatId);
      renderSendButton(chatId);
      renderTabs();
      attemptFlush(chatId, false);   // next queued message goes out
      break;
    case "error":
      cs.churn = mkChurn(cs, false) || cs.churn;
      cs.discarding = false;
      cs.running = false;
      cs.live = null;
      cs.tools = {};
      cs.error = ev.msg;
      renderSendButton(chatId);
      queueThreadRedraw(chatId);
      renderTabs();
      break;
  }
}

/* re-pull the persisted chat (the loop saves after every turn) without
 * disturbing the composer */
async function refreshChatSilently(chatId) {
  const res = await Api.call("chat_get", chatId, chatState(chatId).windowSize);
  if (!res.ok) return;
  const cs = chatState(chatId);
  cs.chat = res.data.chat;
  cs.running = res.data.running;
  if (cs.lastStats) {
    const last = [...cs.chat.messages].reverse().find((m) => m.role === "assistant");
    if (last) Object.assign(last, cs.lastStats);
    cs.lastStats = null;
  }
  renderCtxChip(chatId);
  renderAttachBar(chatId);   // artifacts ride on the chat doc
  queueThreadRedraw(chatId);
}
