/* chat.js - chat tabs. The message area is modeled after llama.cpp's web
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
    follow: true,        // auto-scroll intent - see the block above atBottom
    followTail: false,   // follower asked for the TAIL of a long reply
  });
}

async function newChat(cloneActive) {
  // Ctrl+N / the + button: a plain new chat with the library defaults.
  // Ctrl+Shift+N from a chat tab: a CLEAN context window with the same
  // working setup - model, permission mode, network, and folder
  // attachments carry over; messages don't. From any other tab the
  // clone shortcut does nothing (there is nothing to clone).
  let template = null;
  if (cloneActive) {
    const active = tabById(st.activeTab);
    if (active?.type !== "chat") return;
    const src = st.chats[active.chatId]?.chat;
    if (src) {
      template = {
        provider: src.provider || "",
        model: src.model || "",
        permMode: src.permMode || "",
        network: netMode(src.network),
        folders: (src.folders || []).map((f) => ({ path: f.path, mode: f.mode })),
      };
    }
  }
  const res = await Api.call("chat_new", template);
  if (!res.ok) { toast(res.error, "err"); return; }
  const c = res.data.chat;
  chatState(c.id).chat = c;
  chatState(c.id).msgBase = 0;
  openTab("chat", c.id);
  renderChat(c.id);
}

/* the window's ABSOLUTE start in the full history. Captured at fetch
 * time: client-side appends during a run grow messages without moving
 * the window start, so `msgBase + i` stays the message's real index. */
function setMsgBase(cs) {
  const n = (cs.chat.messages || []).length;
  cs.msgBase = Math.max(0, (cs.chat.totalMessages ?? n) - n);
}

async function openChat(chatId) {
  const res = await Api.call("chat_get", chatId, chatState(chatId).windowSize);
  if (!res.ok) { toast(res.error, "err"); return; }
  const cs = chatState(chatId);
  cs.chat = res.data.chat;
  cs.running = res.data.running;
  setMsgBase(cs);
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
  // up - which is also exactly when auto-scroll is paused; clicking it
  // lands on the last message and re-arms auto-scroll
  const jump = el("button", {
    class: "jump-bottom", title: "Jump to the latest message",
    html: icon("down", 15),
  });
  jump.addEventListener("click", () => {
    const cs = chatState(chatId);
    cs.follow = true;                  // an explicit "take me to the tail"
    cs.followTail = true;
    cs.atBottom = true;
    stickBottom(thread, cs);
    updateJump();
  });
  const updateJump = () => jump.classList.toggle("show", !atBottom(thread));
  thread.addEventListener("scroll", updateJump, { passive: true });
  // an UPWARD wheel flick disarms instantly, position notwithstanding -
  // waiting for the position to cross the threshold let fast streams
  // yank the view back mid-gesture and fight the reader
  thread.addEventListener("wheel", (e) => {
    if (e.deltaY >= 0 || !thread.clientHeight) return;
    const cs = chatState(chatId);
    cs.follow = false;
    cs.followTail = false;
    cs.atBottom = false;
  }, { passive: true });
  thread.addEventListener("scroll", () => {
    // a hidden panel reads scrollTop 0 - never let that clobber the real
    // position (it's restored when the tab activates again)
    if (!thread.clientHeight) return;
    const cs = chatState(chatId);
    cs.scrollPos = thread.scrollTop;   // tab switches + session
    // BOTTOM-NESS is what tab switching must preserve: at-bottom means
    // "keep following the stream", scrolled-up means "hold my spot".
    // Only USER scrolls speak for intent - programmatic writes
    // (progScroll) never re-arm or disarm the follow flag. Scrolling to
    // the very bottom means "give me the TAIL" (past any read-from-the-
    // start hold); anywhere else is manual control.
    if (!cs._progScroll) {
      cs.follow = atBottom(thread);
      cs.followTail = cs.follow;
      cs.atBottom = cs.follow;
    }
    saveSession();
  }, { passive: true });
  panel._updateJump = updateJump;
  // window resizes change the fold: a follower keeps the newest content
  // in frame (holders are position-stable and need nothing)
  window.addEventListener("resize", () => {
    const cs = st.chats[chatId];
    if (cs && thread.clientHeight && chatFollows(cs)) {
      followScroll(thread, cs);
    }
  });
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
    const before = input.style.height;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
    // a growing composer shrinks the thread viewport - a follower must
    // not lose the newest line behind it
    if (input.style.height !== before && chatFollows(chatState(chatId))) {
      requestAnimationFrame(() => followScroll(thread, chatState(chatId)));
    }
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
    // PgUp/PgDn from the input scroll the THREAD - the eyes are up there
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
    title: "Provider and model for this chat  (Ctrl+.)",
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
    title: "Context usage - hover for the breakdown",
  });
  wireCtxHover(ctxBtn, chatId);

  const thoughtBtn = el("button", {
    class: "perm-pill think-chip", "data-role": "thoughts",
    title: "Per-chat thought truncation - how much stored thinking rides "
      + "the wire  (Ctrl+])",
  });
  setHotkey(thoughtBtn, "Ctrl+]");
  thoughtBtn.addEventListener("click", async () => {
    const cs = chatState(chatId);
    const res = await Api.call("chat_set_thought_truncation", chatId,
      !chatThoughtTrunc(cs));
    if (!res.ok) { toast(res.error, "err"); return; }
    cs.chat.thoughtTruncation = res.data.thoughtTruncation;
    renderThoughtChip(chatId);
    refreshChatSilently(chatId);   // the context estimate changes
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
      ctxBtn, thoughtBtn, modelBtn, btnSend));
  box.addEventListener("click", (e) => {
    if (e.target === box) input.focus();   // dead space focuses the input
  });

  // drag & drop images from the file system onto the input panel.
  // Browser drops carry file BYTES, not paths - each image is staged to
  // ~/.loom/attachments through the bridge and attached by path.
  const IMG_EXT = /\.(png|jpe?g|webp|gif|bmp)$/i;
  box.addEventListener("dragover", (e) => {
    e.preventDefault();
    // the library-icon drag advertises effectAllowed "copy" - the drop is
    // refused unless the target's dropEffect agrees
    if (st.dragLibrary) e.dataTransfer.dropEffect = "copy";
    box.classList.add("drag-over");
  });
  box.addEventListener("dragleave", () => box.classList.remove("drag-over"));
  box.addEventListener("drop", async (e) => {
    e.preventDefault();
    box.classList.remove("drag-over");
    // the topbar library icon dragged in: attach the whole library folder
    // read-only - the pill's view/write toggle opens it up for the model
    // to work on its own prompts/knowledge/tools
    const isLibraryDrag = st.dragLibrary
      || (st.library && e.dataTransfer?.getData("text/plain") === st.library);
    st.dragLibrary = false;
    if (isLibraryDrag && st.library) {
      attachFolderPath(chatId, st.library);
      return;
    }
    const cs = chatState(chatId);
    // OS drags carry file:// URIs - the only drop form with REAL PATHS.
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
        toast(baseName(path) + " - single files don't attach; drop its "
          + "FOLDER to let the model read it, or drop images.", "warn");
      }
    }
    // the qt backend RECORDS the native paths of this drop (see
    // drop_paths in app.py) - that is the only reliable source of real
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
          toast(baseName(p) + " - single files don't attach; drop its "
            + "FOLDER to let the model read it, or drop images.", "warn");
        }
      }
    }
    if (handled) return;
    for (const f of e.dataTransfer?.files || []) {
      // pathless drop (e.g. an image dragged out of a web page): the
      // byte-based staging fallback
      if (!(f.type?.startsWith("image/") || IMG_EXT.test(f.name))) {
        toast(f.name + " - drop a folder to attach it, or images "
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
  const autogenEl = el("div", { class: "autogen-bar", "data-role": "autogen" });
  const queueEl = el("div", { class: "send-queue", "data-role": "queue" });
  inner.append(autogenEl, queueEl, box);
  composer.append(inner);
  const toolsBar = el("div", { class: "chat-toolsbar", "data-role": "toolsbar" });
  panel.append(toolsBar, threadWrap, composer);
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

/* attach a host folder to a chat (view mode unless told otherwise) -
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
  // artifact pills live in the tools bar's artifacts panel now - the
  // attach bar is for what YOU bring (folders, images); the timeline
  // entries in the chat itself still mark every delivery
  renderChatToolsbar(chatId);   // keep the artifacts count fresh
  updateGitBadges(chatId);   // fill "@ <branch>" on the folder pills
}

/* ---------- provider + model resolution (mirrors the backend) ---------- */
function modelKey(provider, model) { return provider + "::" + model; }

/* provider registry state → the dot classes the css already knows */
function providerDot(name) {
  const s = st.providers?.[name]?.state;
  return s === "ok" ? "running" : s === "error" ? "error" : "stopped";
}

function providerModels(name) {
  return st.providers?.[name]?.models || [];
}

/* the endpoint this chat resolves to: {provider, model} - chat's own
 * choice, else the config default, else the first provider (and its
 * first probed model) */
function activeEndpoint(cs) {
  const provs = (st.config && !st.config.error && st.config.providers) || [];
  const pname = cs?.chat?.provider || st.config?.chat?.provider || "";
  const prov = provs.find((p) => p.name === pname)
    || (pname ? null : provs[0]);
  if (!prov) return { provider: pname, model: cs?.chat?.model || "" };
  let model = cs?.chat?.model || "";
  if (!model && !cs?.chat?.provider) model = st.config?.chat?.model || "";
  if (!model) model = providerModels(prov.name)[0]?.id || "";
  return { provider: prov.name, model };
}

function renderModelButton(chatId) {
  const panel = chatPanel(chatId);
  const btn = panel?.querySelector('[data-role="model"]');
  if (!btn) return;
  const ep = activeEndpoint(st.chats[chatId]);
  const label = ep.provider
    ? (ep.model ? ep.provider + " · " + ep.model : ep.provider + " · (no model)")
    : "no providers";
  // at-a-glance thinking level: the configured reasoning level's single
  // word (off/on/low/medium/xhigh/…); nothing shown on server default
  const key = modelKey(ep.provider, ep.model);
  const lvl = st.reasoning?.[key]?.level;
  btn.replaceChildren(...[
    el("span", { class: "dot " + providerDot(ep.provider) }),
    el("span", { class: "mname", text: label }),
    lvl ? el("span", { class: "mlvl", text: lvl,
                       title: "Reasoning: " + reasoningLabel(st.reasoning[key]) }) : null,
    el("span", { class: "caret", text: "▾" }),
  ].filter(Boolean));
}

/* three network modes, cycled by the chip: none → loopback → on.
 * Legacy chats carry booleans - normalize everywhere. */
function netMode(v) {
  if (v === true || v === "on") return "on";
  if (v === "loopback") return "loopback";
  return "none";
}

/* the network control lives in the tools bar now (a popout panel) */
function renderNetChip(chatId) {
  renderChatToolsbar(chatId);
}

/* effective per-chat thought truncation: the chat's own value, else the
 * loom.yaml default (older chats predate the per-chat setting) */
function chatThoughtTrunc(cs) {
  const v = cs?.chat?.thoughtTruncation;
  return v == null ? st.config?.chat?.thought_truncation !== false : !!v;
}

function renderThoughtChip(chatId) {
  const btn = chatPanel(chatId)?.querySelector('[data-role="thoughts"]');
  if (!btn) return;
  const trunc = chatThoughtTrunc(chatState(chatId));
  btn.classList.toggle("off", !trunc);
  btn.replaceChildren(el("span", {
    text: trunc ? "latest thought" : "all thoughts" }));
  btn.title = (trunc
    ? "Only the latest turn's thinking rides the wire - click to keep "
      + "every stored thought (context-hungry)"
    : "EVERY stored thought rides the wire - click to keep only the "
      + "latest turn's") + "  (Ctrl+])";
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
  btn.title = "Container for shell commands - "
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
    : "No environment - pick one to load its variables (API keys…) into "
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

/* refresh every open chat tab's model button (and a live popup) - called
 * on config edits and on every server state event */
function refreshChatModelSelectors() {
  for (const tab of st.tabs) {
    if (tab.type !== "chat") continue;
    renderModelButton(tab.chatId);
    renderPermPill(tab.chatId);
  }
  if (window._modelPopupRefresh) window._modelPopupRefresh();
}

/* ---------- the model menu: provider → model → reasoning ----------
 * Three layers of submenus, fully keyboard-driven: ↑/↓ walk (starting
 * from the ACTIVE row), → descends, ← ascends, Enter picks, Esc closes.
 * Models come LIVE from each provider's API (cached from the last probe;
 * opening a provider re-pulls its list). Every model row also carries a
 * brain button → that model's reasoning submenu: first HOW to configure
 * it (default / reasoning_effort request field / enable_thinking template
 * kwarg / think prompt switch), then the level. */
const REASONING_METHODS = [
  { id: "effort", name: "reasoning_effort",
    sub: "graded - the OpenAI-style request field (Anthropic providers "
      + "map it to a thinking budget)",
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
  const provs = (st.config && !st.config.error && st.config.providers) || [];
  const ep = activeEndpoint(chatState(chatId));
  // one provider: no point in a one-row first layer - open on its models
  let view = provs.length === 1
    ? { mode: "models", prov: provs[0].name }
    : { mode: "prov" };
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
      class: "iconbtn", title: "Re-probe providers", html: icon("refresh", 14),
      onclick: () => {
        if (view.mode === "models") loadModels(view.prov, true);
        else Api.call("providers_refresh");
      },
    });
    m.classList.add("model-popup");   // long model names need the room
    const head = el("div", { class: "popup-head" }, filterIn, refreshBtn);
    const list = el("div", { class: "popup-list" });
    m.append(head, list);
    m._list = list;
    m._head = head;
    m._filterIn = filterIn;
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

  /* re-seed the keyboard cursor on the active row after each re-render -
   * arrows must always start from the highlighted value */
  function reseed() {
    menu.querySelectorAll(".popup-item.kbd-sel")
      .forEach((n) => n.classList.remove("kbd-sel"));
    menu._kbdSetSel?.(menu.querySelector(".popup-item.sel"));
  }

  function render() {
    const filterable = view.mode === "models" || view.mode === "prov";
    menu._head.classList.toggle("hidden", !filterable);
    menu._back = null;
    if (view.mode === "prov") renderProviders();
    else if (view.mode === "models") renderModels();
    else renderReason();
    reseed();
    if (filterable) setTimeout(() => menu._filterIn.focus(), 0);
  }

  function backRow(list, label, fn) {
    menu._back = fn;   // ← ascends from anywhere in this layer
    const row = el("div", { class: "popup-item", onclick: fn },
      el("span", { class: "pi-name", text: "← " + label }));
    list.append(row);
    return row;
  }

  function renderProviders() {
    const list = menu._list;
    list.replaceChildren();
    if (!provs.length) {
      list.append(
        el("div", { class: "popup-empty", text: "No providers in loom.yaml yet." }),
        el("div", { class: "popup-cta" },
          el("button", {
            class: "btn btn-sm btn-acc", text: "Add provider…",
            title: "Point Loom at a llama-server or a hosted vendor API",
            onclick: () => { close(); openTab("servers"); setTimeout(() => providerDialog(), 50); },
          })));
      return;
    }
    for (const p of provs.filter((x) => matches(x.name))) {
      const stt = st.providers?.[p.name] || {};
      const row = el("div", {
        class: "popup-item" + (p.name === ep.provider ? " sel" : ""),
        "data-submenu": "",
        onclick: () => { view = { mode: "models", prov: p.name }; filter = ""; menu._filterIn.value = ""; render(); loadModels(p.name); },
      },
        el("span", { class: "dot " + providerDot(p.name) }),
        el("span", { class: "pi-col" },
          el("span", { class: "pi-name", text: p.name }),
          el("span", { class: "pi-reason",
            text: (p.vendor || "llama-cpp")
              + (p.ssh ? " · ssh " + p.ssh : "")
              + (stt.state === "ok" ? " · " + (stt.models?.length || 0) + " model(s)"
                 : stt.state === "error" ? " · unreachable" : "") })),
        el("span", { class: "caret", text: "▸" }));
      list.append(row);
    }
  }

  const loading = {};   // provName -> in-flight guard (stops retry loops)
  async function loadModels(provName, force) {
    if (!force && providerModels(provName).length) return;
    if (loading[provName]) return;
    loading[provName] = true;
    const r = await Api.call("provider_models", provName);
    delete loading[provName];
    st.providers = st.providers || {};
    // an ERROR is a state too - leaving the registry empty made the
    // empty-list render re-request forever
    st.providers[provName] = r.ok
      ? { ...(st.providers[provName] || {}),
          state: r.data.state, detail: r.data.detail, models: r.data.models }
      : { ...(st.providers[provName] || {}),
          state: "error", detail: r.error, models: [] };
    if (view.mode === "models" && view.prov === provName) render();
  }

  function renderModels() {
    const provName = view.prov;
    const list = menu._list;
    list.replaceChildren();
    if (provs.length > 1) {
      backRow(list, "Providers", () => { filter = ""; menu._filterIn.value = ""; view = { mode: "prov" }; render(); });
    }
    const stt = st.providers?.[provName];
    // some vendors (Vertex AI) list no models at all - the id is typed
    const typedRow = () => el("div", {
      class: "popup-item",
      onclick: () => {
        close();
        promptModal("Model id · " + provName,
          "The exact id this provider serves (e.g. "
          + "gemini-2.0-flash, claude-sonnet-4-5, gpt-4o).",
          chatState(chatId).chat?.model || "", async (v) => {
            const id = v.trim();
            if (!id) return;
            const cs2 = chatState(chatId);
            cs2.chat.provider = provName;
            cs2.chat.model = id;
            const r = await Api.call("chat_set_model", chatId, provName, id);
            if (!r.ok) { toast(r.error, "err"); return; }
            refreshChatSilently(chatId);
            renderModelButton(chatId);
          }, "Use model");
      },
    },
      el("span", { class: "pi-name", text: "✎ Type a model id…" }));
    const models = providerModels(provName).filter((mo) => matches(mo.id));
    if (!models.length) {
      list.append(el("div", { class: "popup-empty",
        text: !stt ? "Asking " + provName + " for its models…"
          : stt.state === "error" ? (stt.detail || "provider unreachable")
          : filter ? "No models match the filter."
          : (stt.detail || "The provider lists no models.") }));
      if (stt && stt.state !== "error") list.append(typedRow());
      if (!stt) loadModels(provName);
      return;
    }
    const pinnedSet = new Set(st.pins || []);
    const ordered = [...models.filter((mo) => pinnedSet.has(modelKey(provName, mo.id))),
                     ...models.filter((mo) => !pinnedSet.has(modelKey(provName, mo.id)))];
    for (const mo of ordered) {
      const key = modelKey(provName, mo.id);
      const pref = reasonMap[key];
      const isCurrent = provName === ep.provider && mo.id === ep.model;
      const brain = el("button", {
        class: "iconbtn brain" + (pref ? " on" : ""),
        title: "Reasoning - " + reasoningLabel(pref)
          + " · click (or →) to configure",
        html: icon("brain", 14),
      });
      const openReason = () => { view = { mode: "method", prov: provName, model: mo.id }; render(); };
      brain.addEventListener("click", (e) => { e.stopPropagation(); openReason(); });
      const isPin = pinnedSet.has(key);
      const pinBtn = el("button", {
        class: "iconbtn pin" + (isPin ? " on" : ""),
        title: isPin ? "Unpin" : "Pin to the top",
        html: icon("pin", 13),
      });
      pinBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const r = await Api.call("pin_set", key, !isPin);
        if (!r.ok) { toast(r.error, "err"); return; }
        st.pins = r.data.pins || [];
        render();
      });
      const row = el("div", {
        class: "popup-item" + (isCurrent ? " sel" : ""),
        onclick: () => {
          const cs2 = chatState(chatId);
          cs2.chat.provider = provName;
          cs2.chat.model = mo.id;
          Api.call("chat_set_model", chatId, provName, mo.id)
            .then(() => refreshChatSilently(chatId));   // nCtx changed
          renderModelButton(chatId);
          close();
        },
      },
        el("span", { class: "dot " + providerDot(provName) }),
        el("span", { class: "pi-col" },
          el("span", { class: "pi-name", text: mo.id }),
          pref ? el("span", { class: "pi-reason",
            text: "reasoning · " + reasoningLabel(pref) }) : null),
        el("span", { class: "pi-sub",
          text: mo.ctx ? fmtTok(mo.ctx) + " ctx" : "" }),
        pinBtn, brain);
      row._submenu = openReason;   // → on a model row opens its reasoning
      list.append(row);
    }
  }

  /* the reasoning submenu, two layers deep */
  function renderReason() {
    const provName = view.prov;
    const modelId = view.model;
    const key = modelKey(provName, modelId);
    const pref = reasonMap[key] || null;
    const list = menu._list;
    list.replaceChildren();
    const item = (attrs, ...kids) => {
      const row = el("div", {
        class: "popup-item" + (attrs.sel ? " sel" : ""),
        ...(attrs.submenu ? { "data-submenu": "" } : {}),
      }, ...kids);
      row.addEventListener("click", attrs.onclick);
      list.append(row);
      return row;
    };
    const setPref = async (p) => {
      const res = await Api.call("reasoning_set", key, p);
      if (!res.ok) { toast(res.error, "err"); return; }
      reasonMap = st.reasoning = res.data.reasoning || {};
      view = { mode: "models", prov: provName };
      render();
      refreshChatModelSelectors();   // every composer button shows the level
    };

    if (view.mode === "method") {
      list.append(el("div", { class: "popup-empty",
        text: "Reasoning - " + modelId }));
      backRow(list, "Models", () => { view = { mode: "models", prov: provName }; render(); });
      item({ sel: !pref, onclick: () => setPref(null) },
        el("span", { class: "pi-name", text: "Default" }),
        el("span", { class: "pi-sub", text: "send nothing - server decides" }));
      for (const meth of REASONING_METHODS) {
        item({
          sel: pref?.method === meth.id, submenu: true,
          onclick: () => { view = { mode: "level", prov: provName, model: modelId, method: meth }; render(); },
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
      text: meth.name + " - " + modelId }));
    backRow(list, "Back", () => { view = { mode: "method", prov: provName, model: modelId }; render(); });
    for (const lv of meth.levels) {
      item({
        sel: pref?.method === meth.id && pref?.level === lv,
        onclick: () => setPref({ method: meth.id, level: lv }),
      },
        el("span", { class: "pi-name", text: lv }));
    }
  }

  render();
  if (view.mode === "models") loadModels(view.prov);
  // live ● updates while open - but never stomp the reasoning submenu
  window._modelPopupRefresh = () => {
    if (view.mode === "prov" || view.mode === "models") render();
  };
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
/* fmtTok lives in util.js - the popped-out diag window needs it too */

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
  // both readings at once - tokens / window (percent) - plus the ring;
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

/* what the NEXT send will cost in prompt processing, from the measured
 * prefill speed of this chat's own server. Two figures: the cached case
 * (only what grew since last turn gets processed - the normal case) and
 * the cold case (a full reprocess: cache evicted, model swapped). Both
 * honest estimates, both marked ~. */
function nextPromptRows(bd) {
  const speed = Number(bd.promptSpeed) || 0;
  const used = Number(bd.usedTokens ?? bd.estTokens) || 0;
  if (!speed || !used) return [];
  const fresh = Math.max(0, (Number(bd.estTokens) || 0)
    - (Number(bd.lastUsedTokens) || 0));
  const fmtS = (tok) => {
    const s = tok / speed;
    return s < 1 ? "<1s" : s < 90 ? "~" + Math.ceil(s) + "s"
      : "~" + Math.round(s / 60) + "m";
  };
  const row = (k, v) => el("div", { class: "ctx-row" },
    el("span", { text: k }), el("b", { text: v }));
  return [
    el("div", { class: "ctx-sep2" }),
    row("Measured prefill speed", Math.round(speed) + " tok/s"),
    row("Next prompt (cache warm)",
      "~" + fmtTok(fresh) + " new tok · " + fmtS(fresh)),
    row("Next prompt (cache cold)",
      "~" + fmtTok(used) + " tok · " + fmtS(used)),
  ];
}

/* the one open ctx card (hover-only) - closed on tab switches */
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
      let s = String(v ?? "·");
      if (s === "null" || s === "undefined" || s === "NaN") s = "·";
      return el("div", { class: "ctx-row" },
        el("span", { text: k }), el("b", { text: s }));
    };
    const p = bd.parts || {};
    const pct = Number.isFinite(Number(bd.pct)) ? Number(bd.pct) : null;
    const nctx = Number(bd.nCtx) || 0;
    // NATIVE append() stringifies null into a literal "null" TEXT NODE -
    // the source of the phantom nulls. Conditional children must be
    // filtered before they ever reach append().
    // The ROWS live in their own scrollable region: long chats grow the
    // breakdown past the popup's max-height, and without this the
    // overflow shoved the action buttons off the card entirely.
    const rowsBox = el("div", { class: "ctx-rows" });
    rowsBox.append(...[
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
        bd.lastUsedTokens ? fmtTok(bd.lastUsedTokens) : "- (no turns yet)"),
      row("Context window", nctx
        ? fmtTok(nctx) + (pct != null ? ` (${pct.toFixed(1)}% used)` : "")
        : "unknown - model not in loom.yaml"),
      el("div", { class: "ctx-sep2" }),
      row("Auto-compaction", bd.auto
        ? "on at " + Math.round((Number(bd.threshold) || 0.8) * 100) + "%"
        : "off"),
      // the predictive trigger's inputs: what recent turns actually cost
      bd.turnSamples ? row("Recent turn growth",
        "~" + fmtTok(bd.turnAvg) + "/turn · peak " + fmtTok(bd.turnMax)) : null,
      bd.auto && bd.headroom ? row("Reserved headroom",
        fmtTok(bd.headroom)) : null,
      ...nextPromptRows(bd),
    ].filter(Boolean));
    card.append(rowsBox);
    // Compact now LEFT, Diagnostics RIGHT - compaction mutates the
    // conversation, so it must never sit where the harmless "show me
    // the numbers" click lands
    card.append(el("div", { class: "ctx-actions" },
        el("button", {
          class: "btn btn-sm", text: "Compact now",
          title: "Summarize the conversation now and replace the older "
            + "turns on the wire",
          onclick: async () => {
            const res = await Api.call("chat_compact", chatId);
            if (!res.ok) toast(res.error, "err");
            card?.remove(); card = null;
          },
        }),
        el("span", { class: "spacer" }),
        el("button", {
          class: "btn btn-sm", text: "Diagnostics",
          title: "Per-entry token graph + table for this chat",
          onclick: () => { destroy(); openDiagTab(chatId); },
        })));
    document.body.append(card);
    const a = btn.getBoundingClientRect();
    // the card sits ABOVE the chip: let it use that space (overriding
    // the generic popup cap), never more - the rows scroll if a very
    // long chat still can't fit, and the buttons always stay on board
    card.style.maxHeight = Math.max(140, Math.min(640, a.top - 16)) + "px";
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
  renderThoughtChip(chatId);
  renderModelButton(chatId);
  renderPermPill(chatId);
  renderContainerPill(chatId);
  renderEnvPill(chatId);
  renderAttachBar(chatId);
  renderAutoGen(chatId);
  renderChatToolsbar(chatId);
  renderChatThread(chatId);   // consumes cs.restoreScroll when visible
  renderSendButton(chatId);
}

function renderSendButton(chatId) {
  const panel = chatPanel(chatId);
  if (!panel) return;
  const cs = chatState(chatId);
  const b = panel.querySelector('[data-role="send"]');
  const busy = !!(cs.running || cs.compacting);   // compaction stops too
  b.classList.toggle("stop", busy);
  // stop is a FILLED square (currentColor = black via .stop), not the
  // stroked outline the icon set draws
  b.innerHTML = busy
    ? '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
      + '<rect x="7" y="7" width="10" height="10" rx="1.5"'
      + ' fill="currentColor"/></svg>'
    : icon("up");
  b.title = busy
    ? (cs.running ? "Stop generating  (Esc)" : "Cancel compaction  (Esc)")
    : "Send  (Enter)";
  // only the stop state earns a chip - Enter-to-send is placeholder lore
  if (busy) setHotkey(b, "Esc");
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
    ? "A tool call is waiting - Ctrl+Enter allows it, Ctrl+Esc denies it."
    : cs.running
      ? "Generating - Esc stops it. Enter queues your next message."
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
/* the loop retried an answerless turn 3 times and got nowhere - honesty
 * with a shrug */
const CHURN_FAIL_PHRASES = [
  "Gave up after", "Threw in the towel after", "Ran out of steam after",
  "Thought itself into a corner for", "Waved the white flag after",
  "Lost the thread after",
];

function mkChurn(cs, kind) {
  if (!cs.respT0) return null;
  const ms = performance.now() - cs.respT0;
  cs.respT0 = null;
  // a failed run deserves its eulogy even when it died fast
  if (ms < 1000 && kind !== "fail") return null;
  const pool = kind === "cancel" ? CHURN_CANCEL_PHRASES
    : kind === "fail" ? CHURN_FAIL_PHRASES : CHURN_PHRASES;
  const phrase = pool[Math.floor(Math.random() * pool.length)];
  return { text: phrase + " " + fmtDur(Math.round(ms)) + "."
    + (kind === "fail"
      ? " The model kept ending without an answer - try Retry, or a "
        + "different model." : "") };
}

/* ---------- live-stream liveness ----------
 * A token count rides after the streaming cursor and ticks up with every
 * delta (thinking included). Its heartbeat IS the health indicator: a
 * frozen number means the stream stalled - no more wondering. */
function liveTok(cs) {
  return Math.round(((cs.live?.text || "").length
    + (cs.live?.think || "").length) / 4);
}

/* before the first token the server is PREFILLING (reading the whole
 * prompt). Servers speaking `return_progress` stream real progress -
 * tokens processed, cache reuse, and enough to compute an ETA; others
 * get a ticking clock so slow-to-first-token reads busy, not broken.
 * While generating, per-chunk timings put a LIVE tok/s next to the
 * count. */
function liveCountText(cs) {
  const n = liveTok(cs);
  if (n > 0) {
    const tps = cs.liveTimings?.predicted_per_second;
    return "~" + fmtTok(n) + " tok"
      + (tps ? " · " + (tps >= 100 ? Math.round(tps) : tps.toFixed(1)) + " tok/s" : "");
  }
  const p = cs.progress;
  if (p && p.total > 0 && p.processed != null) {
    const cache = Math.min(p.cache || 0, p.total);
    const done = Math.max(0, p.processed - cache);
    const todo = Math.max(1, p.total - cache);
    const pct = Math.min(100, Math.round(100 * done / todo));
    let eta = "";
    if ((p.time_ms || 0) > 400 && done > 0 && done < todo) {
      const left = (todo - done) / (done / (p.time_ms / 1000));
      eta = " · ~" + (left >= 90 ? Math.round(left / 60) + "m" : Math.ceil(left) + "s")
        + " left";
    }
    return "reading prompt " + pct + "% - " + fmtTok(done) + "/" + fmtTok(todo)
      + " tok" + (cache ? " (+" + fmtTok(cache) + " cached)" : "") + eta;
  }
  const secs = cs.turnT0
    ? Math.max(0, (performance.now() - cs.turnT0) / 1000) : 0;
  return "reading prompt… " + secs.toFixed(0) + "s";
}

/* The model may ECHO its own [.. UTC] signal stamp while streaming; the
 * persisted turn strips it (chat.py) but the LIVE view must never show
 * it either - signals do not belong in the visible message. The stamp
 * can arrive split across deltas, so the accumulated text is re-checked
 * every time, and a plausible partial prefix ("[2026-09-0") is HELD
 * BACK rather than flashed and yanked. */
const LIVE_STAMP_RE = /^\s*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\]\s*/;
const LIVE_STAMP_PARTIAL_RE = /^\s*\[[\d\- :UTC]{0,20}$/;

function liveVisibleText(t) {
  t = String(t || "");
  const m = LIVE_STAMP_RE.exec(t);
  if (m) return t.slice(m[0].length);
  return LIVE_STAMP_PARTIAL_RE.test(t) ? "" : t;
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

/* the prefill clock only moves if something re-renders it - a light
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
 * output - the UI stops NOW and stale events are discarded while the
 * backend unwinds. Returns false when nothing was running. */
function stopChatGeneration(chatId) {
  const cs = chatState(chatId);
  if (!cs.running && !cs.compacting) return false;
  Api.call("chat_stop", chatId);
  cs.churn = mkChurn(cs, "cancel") || cs.churn;
  cs.discarding = true;
  cs.running = false;
  cs.compacting = false;
  cs.compactTok = 0;
  cs.live = null;
  cs.progress = null;
  cs.liveTimings = null;
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

/* Ctrl+I from anywhere: land in a message input - the active chat's, or
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

/* ---------- deterministic auto-scroll ----------
 * ONE per-chat flag (cs.follow) is the whole truth, and only EXPLICIT
 * user actions change it:
 *   arm    - scrolling to the bottom, the jump button, sending a
 *            message (Enter - queued or not), opening at the bottom
 *   disarm - scrolling away from the bottom
 * Programmatic scrolls go through progScroll(), which the scroll
 * listener recognizes and NEVER treats as intent. Every DOM change then
 * applies one rule: following → stick to the bottom; not following →
 * the view must not move (full rebuilds restore the entry that was at
 * the top of the viewport; in-place toggles compensate by height delta
 * when the toggled block sits above the viewport). */
function chatFollows(cs) { return cs.follow !== false; }

function progScroll(thread, cs, top) {
  cs._progScroll = true;
  thread.scrollTop = top;
  requestAnimationFrame(() => { cs._progScroll = false; });
}

function stickBottom(thread, cs) {
  progScroll(thread, cs, thread.scrollHeight);
}

/* ---------- read-from-the-start following ----------
 * Following does NOT mean welded to the bottom. The follower's view
 * advances with the stream only until the header of the newest reply
 * TEXT would leave the top of the panel - then it holds there, so the
 * newest message is always readable from its beginning while the rest
 * streams in below the fold. Prefill, thoughts, and tool activity are
 * NOT anchors: they follow the tail (thoughts stream and collapse the
 * way they always did); only message text pins.
 *
 * The two readers this serves:
 *   walk-away  - returns to the reply pinned at the top, reads down.
 *   take-over  - scrolls once text passes the fold: ANY scroll takes
 *                manual control (the view then never moves on its own);
 *                scrolling to the very bottom (or the jump button) asks
 *                for the TAIL instead - cs.followTail sticks to the true
 *                bottom until the next reply begins, which re-clamps.
 * Sending a message always resets to read-from-the-start following. */
function genStartEl(thread, cs) {
  if (cs.live) {
    // streaming: ONLY the reply TEXT anchors. Prefill, thoughts and
    // tool phases return null → tail behavior, so thoughts stream and
    // collapse exactly like they always did. cs.live resets per turn
    // (tool_result), so in a tool loop every intermediate message
    // clamps at ITS start while it streams and the final message is
    // what ends up held.
    return thread.querySelector('[data-skey="live-msg"]');
  }
  // idle: the newest persisted reply's header
  const entries = thread.querySelectorAll(".msg.assistant");
  return entries.length ? entries[entries.length - 1] : null;
}

function followScroll(thread, cs) {
  const bottom = Math.max(0, thread.scrollHeight - thread.clientHeight);
  let target = bottom;
  if (!cs.followTail) {
    const start = genStartEl(thread, cs);
    if (start) target = Math.min(bottom, Math.max(0, start.offsetTop - 8));
  }
  progScroll(thread, cs, target);
}

/* the entry at the top of the viewport + its offset - the thing the
 * reader's eye is on; restored after a full rebuild by its stable key */
function captureAnchor(thread) {
  if (!thread.clientHeight) return null;
  for (const c of thread.children) {
    if (c.offsetTop + c.offsetHeight > thread.scrollTop) {
      return c.dataset.skey
        ? { key: c.dataset.skey, off: c.offsetTop - thread.scrollTop }
        : null;
    }
  }
  return null;
}

function restoreAnchor(thread, anchor) {
  if (!anchor) return;
  const el2 = [...thread.children]
    .find((c) => c.dataset.skey === anchor.key);
  if (el2) thread.scrollTop = Math.max(0, el2.offsetTop - anchor.off);
}

/* after an in-place expand/collapse: following sticks; otherwise the
 * scroll compensates when the resized block is above the viewport */
function settleAfterToggle(chatId, node, beforeH) {
  const cs = chatState(chatId);
  const thread = chatPanel(chatId)?.querySelector('[data-role="thread"]');
  if (!thread || !thread.clientHeight) return;
  if (chatFollows(cs)) { followScroll(thread, cs); return; }
  const delta = thread.scrollHeight - beforeH;
  if (!delta) return;
  const nr = node.getBoundingClientRect();
  const tr = thread.getBoundingClientRect();
  if (nr.top < tr.top) progScroll(thread, cs, thread.scrollTop + delta);
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
  // out from under a live selection - retry once it's gone
  if (selectionWithin(thread)) {
    clearTimeout(cs._selRetry);
    cs._selRetry = setTimeout(() => renderChatThread(chatId), 1000);
    return;
  }
  // deterministic scroll: intent from cs.follow only. Not following →
  // capture the entry under the reader's eye BEFORE the rebuild and put
  // it back at the same offset after - a redraw must never move the view.
  const follow = chatFollows(cs);
  const anchor = follow ? null : captureAnchor(thread);
  const tag = (node, key) => {
    if (node) node.dataset.skey = key;
    return node;
  };
  thread.replaceChildren();

  const msgs = cs.chat.messages || [];
  // windowed history: only the fetched tail is in msgs; older messages
  // load on demand (keeps 100k-message chats usable - render and bridge
  // transfer stay O(window))
  const hidden = Math.max(0, (cs.chat.totalMessages ?? msgs.length) - msgs.length);
  if (hidden > 0) {
    thread.append(tag(el("div", { class: "chat-resume" },
      el("button", {
        class: "btn btn-sm",
        text: `↑ Show earlier messages (${hidden.toLocaleString()} hidden)`,
        onclick: () => loadEarlierMessages(chatId),
      })), "earlier"));
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
  const base = cs.msgBase ?? hidden;   // window start, as an absolute index
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (m.role === "user") {
      flushStats();
      thread.append(tag(userMsgEl(chatId, m, base + i), "m" + (base + i)));
    } else if (m.role === "assistant") {
      flushStats();
      // the THOUGHT is its own entry above the message, carrying the
      // model's name and its own size when expanded
      if (m.thinking) {
        thread.append(tag(
          thinkEntryEl(chatId, m, cs.chat.model, i, base + i),
          "th" + (base + i)));
      }
      const node = assistantMsgEl(chatId, m, cs.chat.model, i, base + i);
      if (node) thread.append(tag(node, "m" + (base + i)));
      if (m.timings || m.usage) {
        const row = tag(statsRow(m), "st" + (base + i));
        const calls = new Set((m.tool_calls || []).map((c) => c.id));
        if (calls.size) {
          // this row will land under tool cards - align it with THEIR
          // centered, narrower column instead of the message nudge
          row.classList.add("after-tools");
          pendingStats = { row, calls };
        } else {
          thread.append(row);
        }
      }
    } else if (m.role === "tool") {
      thread.append(tag(toolCardEl(chatId, {
        callId: m.tool_call_id, tool: m.name,
        args: argsOfCall(msgs, i, m.tool_call_id),
        state: m.cancelled ? "cancelled"
          : m.ok === false ? "failed" : "done",
        result: m.content,
        msgIdx: base + i,   // persisted card: copy/fork actions apply
      }), "m" + (base + i)));
      if (pendingStats) {
        pendingStats.calls.delete(m.tool_call_id);
        if (!pendingStats.calls.size) flushStats();
      }
    } else if (m.role === "compact") {
      flushStats();
      thread.append(tag(compactCardEl(chatId, m, i), "m" + (base + i)));
    } else if (m.role === "artifact") {
      flushStats();
      thread.append(tag(artifactMsgEl(chatId, m), "m" + (base + i)));
    }
  }
  if (cs.compacting) {
    // the token count lives in its own span so compact_tick can update it
    // without nuking the cancel button next to it
    const cancelBtn = el("button", { class: "btn btn-sm",
      text: "✕ cancel", title: "Cancel compaction  (Esc)" });
    cancelBtn.addEventListener("click", () => stopChatGeneration(chatId));
    thread.append(el("div", { class: "sysnote", "data-role": "compacting" },
      el("span", { "data-role": "compact-tok",
        text: "Compacting context… ~" + fmtTok(cs.compactTok || 0)
          + " tok summarized " }),
      cancelBtn));
  }

  // live streaming block - the cursor FOLLOWS the phases, in order:
  //   prefill  → a bare cursor line (no header yet; elapsed time ticks)
  //   thinking → the thought entry, EXPANDED, cursor inside it
  //   message  → thought collapses, the model-name header appears, and
  //              the cursor moves into the streaming message
  if (cs.live) {
    const name = agentName();
    const visText = liveVisibleText(cs.live.text);
    const inMessage = !!visText;
    const inThink = !!cs.live.think && !inMessage;
    if (!cs.live.think && !inMessage) {
      // prefill (or a held-back stamp echo): just the heartbeat
      thread.append(tag(el("div", { class: "msg live-wait" },
        el("span", { class: "cursor" })), "live-wait"));
    }
    if (cs.live.think) {
      const t = el("div", {
        class: "think" + (inThink ? "" : " collapsed"),
        "data-role": "live-think",
      });
      // text NODE first: the fast path updates firstChild.nodeValue so
      // the cursor sibling survives every delta
      t.append(document.createTextNode(cs.live.think));
      if (inThink) t.append(el("span", { class: "cursor" }));
      thread.append(tag(el("div", { class: "msg think-entry" },
        el("div", { class: "think-toggle",
          text: inThink ? "▾ " + name + " - thinking…"
                        : "▸ " + name + " thought for a bit" }),
        t), "live-th"));
    }
    if (inMessage) {
      const wrap = el("div", { class: "msg assistant" });
      wrap.append(el("div", { class: "msg-head" },
        el("span", { class: "who", text: name })));
      const body = el("div", { class: "msg-body md-render", "data-role": "live" });
      body.innerHTML = renderMarkdown(visText);
      if (visText.length <= ENHANCE_LIVE_MAX) enhanceCodeBlocks(body);
      body.append(el("span", { class: "cursor" }));
      wrap.append(body);
      thread.append(tag(wrap, "live-msg"));
    }
    // the status readout lives on its OWN static line below the stream -
    // inline it sprinted around with the caret, unreadable on fast models
    thread.append(tag(el("div", { class: "live-status" },
      liveCountEl(cs)), "live-status"));
  }
  // waiting permission cards render inline via tool state (already in tools)
  for (const [callId, t] of Object.entries(cs.tools)) {
    if (t.state === "waiting" || t.state === "running") {
      thread.append(tag(toolCardEl(chatId, { callId, ...t }), "lt" + callId));
    }
  }
  // a turn whose tool calls are still executing: its stats land after
  // the live cards - never before the results they belong with
  flushStats();
  if (cs.running && cs.retryNote) {
    thread.append(el("div", { class: "sysnote", text: cs.retryNote }));
  }
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
      label = "↻ Retry - generate a response";
    } else if (last.role === "assistant" && last.stopped) {
      label = "→ Continue - the response stopped early";
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
      el("div", { text: "Attach images or folders below; shell commands run sandboxed in a container." }),
      el("div", { text: "Ctrl+R lets the model open the conversation - no first message needed." })));
  }
  if (follow) followScroll(thread, cs);
  else restoreAnchor(thread, anchor);
  // an explicit position (tab switch, session restore) beats sticking -
  // the user was comparing chats mid-scroll; put them back exactly there.
  // Only consumable while visible: a hidden panel can't scroll.
  if (cs.restoreScroll != null && thread.clientHeight) {
    // CSSOM trap: assigning Infinity to scrollTop is normalized to 0 -
    // the "restore to bottom" sentinel must become a real pixel value
    thread.scrollTop = Number.isFinite(cs.restoreScroll)
      ? cs.restoreScroll : thread.scrollHeight;
    cs.scrollPos = thread.scrollTop;       // read back the clamped value
    cs.restoreScroll = null;
    cs.follow = atBottom(thread);          // the restored spot IS the intent
    cs.followTail = cs.follow;             // restored bottom = tail intent
    cs.atBottom = cs.follow;
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
  setMsgBase(cs);
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

/* ---------- per-entry actions: copy the content / fork the chat ----------
 * Every message, thought, and tool card gets a hover bar. Copy grabs
 * that entry's text; Fork opens a NEW chat truncated right after the
 * entry (the backend slices the FULL history - absIdx is absolute). */
/* `del` names WHAT delete removes: "message" for the whole entry,
 * "thinking" for just the thought; falsy = no delete button */
function msgActionsEl(chatId, absIdx, getText, what, del) {
  const copyBtn = el("button", { class: "msg-act",
    html: icon("copy", 12), title: "Copy this " + what });
  copyBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const ok = await copyText(getText() || "");
    copyBtn.textContent = ok ? "✓" : "✗";
    setTimeout(() => { copyBtn.innerHTML = icon("copy", 12); }, 1400);
  });
  const forkBtn = el("button", { class: "msg-act", html: icon("fork", 12),
    title: "Fork the chat here - a new chat continues from this "
      + what + "; everything after it stays in this one" });
  forkBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    forkChatAt(chatId, absIdx);
  });
  let delBtn = null;
  if (del) {
    delBtn = el("button", { class: "msg-act msg-act-del",
      html: icon("trash", 12),
      title: "Delete this " + what + " from the history" });
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteChatEntry(chatId, absIdx, what, del);
    });
  }
  return el("div", { class: "msg-actions" }, copyBtn, forkBtn, delBtn);
}

function deleteChatEntry(chatId, absIdx, what, part) {
  if (st.chats[chatId]?.running) {
    toast("Wait for the response to finish first.", "warn");
    return;
  }
  confirmModal("Delete " + what,
    part === "thinking"
      ? "Remove this thought from the history? The reply it belongs to "
        + "stays (unless the thought was all there was), and the model "
        + "will no longer see it."
      : "Remove this " + what + " from the chat history? Tool results "
        + "tied to it go with it, and the model will no longer see any "
        + "of it.",
    "Delete", async () => {
      const r = await Api.call("chat_delete_message", chatId, absIdx,
        part || "message");
      if (!r.ok) { toast(r.error, "err"); return; }
      refreshChatSilently(chatId);
    }, true, "chat-del-msg");
}

async function forkChatAt(chatId, absIdx) {
  const r = await Api.call("chat_fork", chatId, absIdx);
  if (!r.ok) { toast(r.error, "err"); return; }
  const c = r.data.chat;
  const cs = chatState(c.id);
  cs.chat = c;
  cs.running = false;
  cs.msgBase = 0;
  // land at the BOTTOM of the copied history - the fork point is there
  cs.atBottom = true;
  cs.follow = true;
  cs.restoreScroll = Infinity;
  openTab("chat", c.id);
  renderChat(c.id);
  toast("Forked into \"" + c.title + "\""
    + (r.data.toolNote
      ? " - tools ran after this point, so a state-check note was "
        + "added for the model" : ""), "ok", 5000);
}

function toolCopyText(t) {
  const args = typeof t.args === "string" ? t.args
    : JSON.stringify(t.args ?? {}, null, 1);
  const out = t.output || t.result || "";
  return (t.tool || "tool") + " " + (args && args !== "{}" ? args : "()")
    + (out ? "\n\n" + out : "");
}

/* the agent's display name - customizable (chat.assistant_name in
 * loom.yaml), "loom" by default; never the raw model id */
function agentName() {
  return (st.config?.chat?.assistant_name || "loom").trim() || "loom";
}

/* every signal stays user-visible - but OUT of the message body: the
 * speaker name's hover tells exactly what this message reports to the
 * model (and, under time travel, what really happened) */
function whoEl(chatId, label, m, isAssistant, modelId) {
  const w = el("span", { class: "who", text: label });
  const fmt = (ms) => new Date(ms).toISOString()
    .slice(0, 16).replace("T", " ") + " UTC";
  const sig = m.signalTs || m.ts;
  let s;
  if (st.chats[chatId]?.chat?.timeSignalsOff) {
    s = "No time signal - datetime signals are OFF for this chat "
      + "(the time travel panel's toggle).";
  } else if (isAssistant && st.config?.chat?.assistant_signals === false) {
    s = "No time signal - assistant signals are off "
      + "(chat.assistant_signals in loom.yaml).";
  } else if (!sig) {
    s = "No time signal.";
  } else {
    s = "Signals to the model: [" + fmt(sig) + "]"
      + (isAssistant ? " (generation time)" : " (send time)")
      + (m.signalTs && m.signalTs !== m.ts
        ? "\nTime travel - real time: " + fmt(m.ts) : "");
  }
  w.title = (isAssistant && modelId ? "model: " + modelId + "\n" : "") + s;
  return w;
}

function userMsgEl(chatId, m, absIdx) {
  const wrap = el("div", { class: "msg user" });
  wrap.append(el("div", { class: "msg-head" },
    whoEl(chatId, "you", m, false),
    tago(m.ts),
    msgActionsEl(chatId, absIdx, () => m.content || "", "message",
      "message")));
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
 * redraws AND tab switches - tracked in chat state like tool cards. */
function thinkEntryEl(chatId, m, model, idx, absIdx) {
  const openMap = chatState(chatId).thinkOpen
    || (chatState(chatId).thinkOpen = {});
  const key = "think:" + (m.ts || idx);
  const isOpen = !!openMap[key];
  const name = agentName();
  const label = (open) => (open ? "▾ " : "▸ ") + name
    + (open ? " - thinking" : " thought for a bit");
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
      + "reports speed/time per TURN, not per part - those live in the "
      + "stats row at the end of the turn.",
  });
  const tog = el("div", {
    class: "think-toggle", text: label(isOpen),
    onclick: () => {
      // measure BEFORE the toggle: collapsing a tall thought above the
      // viewport must not shift what the reader is looking at
      const thread = chatPanel(chatId)?.querySelector('[data-role="thread"]');
      const beforeH = thread ? thread.scrollHeight : 0;
      const nowOpen = !think.classList.toggle("collapsed");
      if (nowOpen) openMap[key] = true;
      else delete openMap[key];
      tog.textContent = label(nowOpen);
      stats.classList.toggle("hidden", !nowOpen);
      settleAfterToggle(chatId, wrap, beforeH);
    },
  });
  wrap.append(tog, think, stats);
  if (absIdx != null) {
    wrap.append(msgActionsEl(chatId, absIdx,
      () => m.thinking || "", "thought", "thinking"));
  }
  return wrap;
}

function assistantMsgEl(chatId, m, model, idx, absIdx) {
  if (!m.content) return null;  // pure tool turn / thinking-only: no body
  const wrap = el("div", { class: "msg assistant" });
  wrap.append(el("div", { class: "msg-head" },
    whoEl(chatId, agentName(), m, true, model),
    tago(m.ts),
    absIdx != null
      ? msgActionsEl(chatId, absIdx, () => m.content || "", "message",
          "message")
      : null));
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
 * per delta) - those get their final pass when the message persists. */
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

/* ---------- shell cards: like edits/writes, a special-cased preview ----------
 * The command renders as bash (syntax-highlighted, every line - split
 * lines and heredocs stay readable), never as JSON. `$` marks where the
 * command starts. */
function shellCmdEl(cmd, maxLines) {
  const wrap = el("div", { class: "code-prev tool-cmd" });
  const lines = String(cmd ?? "").replace(/\n$/, "").split("\n");
  const cap = maxLines || PREV_MAX_LINES;
  const state = { inBlock: false };
  lines.slice(0, cap).forEach((line, i) => {
    wrap.append(_codeLineEl(line, "sh", state, i === 0 ? "$" : ""));
  });
  if (lines.length > cap) {
    wrap.append(el("div", { class: "cl more" },
      el("span", { class: "ln", text: "" }),
      el("span", { class: "lc",
        text: "… " + (lines.length - cap) + " more lines" })));
  }
  return wrap;
}

/* the END of a shell result - the part that says what actually happened.
 * Shown on the card even when collapsed. The leading "exit N" line is the
 * state chip's job, so it leaves the tail (unless it's all there is). */
function shellTail(result, n = 3) {
  const lines = String(result ?? "").replace(/\s+$/, "").split("\n");
  if (!lines.length || !lines[0]) return "";
  const body = lines.filter((l, i) =>
    !(i === 0 && /^exit -?\d+/.test(l))
    && !/^\[(cancelled|timed out)\]$/.test(l));
  const pick = (body.length ? body : lines).slice(-n);
  return pick.map((l) => (l.length > 200 ? l.slice(0, 200) + "…" : l))
    .join("\n");
}

function toolCardEl(chatId, t) {
  // three resolved outcomes: ok / failed / cancelled (plus denied for a
  // declined permission) - a cancelled command must never read as "ok"
  const stateTxt = {
    announced: "…", waiting: "waiting for permission", running: "running…",
    done: "ok", failed: "failed", denied: "denied", cancelled: "cancelled",
  }[t.state] || t.state;
  const a = toolArgsObj(t);
  const isFileTool = ["read_file", "edit_file", "write_file"].includes(t.tool);
  const isShell = t.tool === "shell";
  // resolved cards collapse to a one-line preview; clicking toggles.
  // Active cards (waiting/running) always render in full.
  const resolved = ["done", "failed", "denied", "cancelled"].includes(t.state);
  const open = !resolved || !!chatState(chatId).toolsOpen[t.callId];
  // shell skips the head summary - its command renders in full below
  const summary = isShell ? "" : String(
    a.path || a.query || "").split("\n")[0].slice(0, 120);
  const card = el("div", {
    class: "tool-card " + (open ? "open" : "closed") + (resolved ? " resolved" : ""),
  });
  const stateCls = t.state === "failed" || t.state === "denied" ? " err"
    : t.state === "cancelled" ? " cancel"
    : t.state === "done" ? " ok" : "";
  const head = el("div", { class: "tool-head" + (resolved ? " toggleable" : "") },
    resolved ? el("span", { class: "tool-chevron", text: open ? "▾" : "▸" }) : null,
    el("span", { html: icon("gear", 13) }),
    el("span", { class: "tool-name", text: t.tool || "tool" }),
    el("span", { class: "tool-state" + stateCls, text: stateTxt }));
  if (summary) head.append(el("span", { class: "tool-path", text: summary, title: summary }));
  if (t.msgIdx != null) {
    // persisted result cards only - live/waiting cards have no index yet
    head.append(msgActionsEl(chatId, t.msgIdx,
      () => toolCopyText(t), "tool call", "message"));
  }
  const toggleCard = () => {
    const openMap = chatState(chatId).toolsOpen;
    if (openMap[t.callId]) delete openMap[t.callId];
    else openMap[t.callId] = true;
    renderChatThread(chatId);
  };
  if (resolved) head.addEventListener("click", toggleCard);
  card.append(head);
  if (isShell && a.command) {
    // even collapsed: the command (capped) + the END of its output -
    // what ran and how it ended, always visible at a glance
    const cmd = shellCmdEl(a.command, open ? 0 : 6);
    card.append(cmd);
    if (!open) {
      cmd.classList.add("toggleable");
      cmd.addEventListener("click", toggleCard);
      const tail = shellTail(t.output || t.result);
      if (tail) {
        const tl = el("div", { class: "tool-tail toggleable", text: tail });
        tl.addEventListener("click", toggleCard);
        card.append(tl);
      }
      return card;
    }
  }
  if (!open) return card;   // the minimal preview row is the whole card

  // file tools and shell show previews, not raw JSON args
  if (!isFileTool && !isShell) {
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

/* artifact delivery: a timeline entry for WHEN the model handed files
 * over. It keeps its own open/save buttons, so a dismissed pill loses
 * nothing - the delivery record stays right here in the history. */
function artifactMsgEl(chatId, m) {
  const items = Array.isArray(m.items) ? m.items : [];
  const card = el("div", { class: "art-msg" },
    el("div", { class: "art-msg-head" },
      el("span", { html: icon("box", 13) }),
      el("span", { text: "artifact" + (items.length > 1 ? "s" : "")
        + " delivered" }),
      tago(m.ts)));
  for (const it of items) {
    card.append(el("div", { class: "art-msg-row" },
      el("span", { html: icon(it.dir ? "box" : "file", 12) }),
      el("button", {
        class: "art-msg-name", text: it.name,
        title: "Open in the preview window",
        onclick: () => { Api.call("artifact_open", chatId, it.name); },
      }),
      el("span", { class: "arc-meta",
        text: (it.dir ? "folder" : fmtBytes(it.bytes)) }),
      el("button", {
        class: "btn btn-sm", text: it.dir ? "zip…" : "save…",
        title: it.dir ? "Save this folder as a zip…" : "Save this file…",
        onclick: async () => {
          const res = await Api.call("artifact_save", chatId, it.name);
          if (!res.ok) { toast(res.error, "err"); return; }
          if (res.data.saved) toast("Saved " + res.data.saved, "ok");
        },
      })));
  }
  return card;
}

/* compaction marker: everything above it left the model's context - the
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
      "Total turn time - prompt processing ("
      + fmtDur(Math.round(t.prompt_ms || 0)) + ") plus generation ("
      + fmtDur(Math.round(t.predicted_ms || 0)) + ")"));
  }
  if (t.predicted_per_second) {
    row.append(stat("tok/s", t.predicted_per_second.toFixed(1),
      "Generation speed - output tokens per second for this turn"));
  }
  if (t.prompt_per_second && t.prompt_n) {
    row.append(stat("pp tok/s", Math.round(t.prompt_per_second),
      "Prompt processing speed - how fast the server read the "
      + t.prompt_n + " NEW prompt tokens this turn (cached tokens are "
      + "free and not counted here)"));
  }
  if (m.ttftMs) {
    row.append(stat("ttft", (m.ttftMs / 1000).toFixed(2) + "s",
      "Time to first token - how long the server processed the prompt "
      + "before anything streamed back"));
  }
  if (u.prompt_tokens != null) {
    const cached = Number(t.cache_n
      ?? u.prompt_tokens_details?.cached_tokens) || 0;
    row.append(stat("prompt",
      fmtTok(u.prompt_tokens) + (cached ? " (" + Math.round(
        100 * cached / Math.max(1, u.prompt_tokens)) + "% cached)" : ""),
      "Prompt tokens - the full context the model read this turn "
      + "(system prompt, history, tool results, tool definitions)"
      + (cached ? ". " + cached + " of them were reused from the "
        + "server's KV cache - only the rest cost prefill time" : "")));
  }
  const outTip = "Output tokens - everything the model generated this "
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
  // viewport - a bottom-follower must be re-stuck or the growth HIDES
  // the newest generated text behind the composer
  const thread = panel.querySelector('[data-role="thread"]');
  const wasBottom = thread && thread.clientHeight && chatFollows(cs);
  host.replaceChildren();
  const canSendNow = true;   // providers take requests whenever reachable
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
      canSendNow ? el("button", {
        class: "btn btn-sm", text: "Send now",
        title: "Move to the front and send immediately",
        onclick: () => {
          const cs2 = chatState(chatId);
          const [it] = cs2.queue.splice(i, 1);
          cs2.queue.unshift(it);
          renderQueue(chatId);
          attemptFlush(chatId, true);
        },
      }) : null,
      el("button", {
        class: "btn btn-sm", text: "×", title: "Cancel this message",
        onclick: () => { cs.queue.splice(i, 1); renderQueue(chatId); },
      })));
  });
  // re-stick after the composer resized (layout settles this frame)
  if (wasBottom) {
    requestAnimationFrame(() => {
      followScroll(thread, cs);
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

/* `item` STAYS at the queue head while the send is in flight - it is
 * only removed on success. A bounced send (racing a still-unwinding
 * cancelled stream) therefore never makes the queued row flicker. */
async function dispatchMessage(chatId, item) {
  const cs = chatState(chatId);
  cs._dispatching = true;
  cs.error = null;
  const res = await Api.call("chat_send", chatId, item.text, item.images || []);
  cs._dispatching = false;
  if (!res.ok) {
    // the item never left the queue - nothing is ever lost. Losing a
    // race with a still-unwinding stream is not an error - and the done
    // event may ALREADY have fired, so never depend on it: retry
    // shortly until the worker is really gone
    if (/already streaming/i.test(res.error || "")) {
      setTimeout(() => attemptFlush(chatId, false), 800);
    } else {
      toast(res.error, "err");
    }
    return;
  }
  const qi = cs.queue.indexOf(item);
  if (qi >= 0) cs.queue.splice(qi, 1);
  renderQueue(chatId);
  cs.chat.messages.push(res.data.message);
  cs.running = true;
  cs.respT0 = performance.now();
  cs.churn = null;
  cs.retryNote = null;
  cs.turnT0 = performance.now();
  cs.live = { text: "", think: "" };
  cs.progress = null;
  cs.liveTimings = null;
  cs.tools = {};
  renderChatThread(chatId);
  renderSendButton(chatId);
  renderTabs();
}

/* dispatch the queue head if possible - the provider takes the request
 * or errors honestly; nothing to start, nothing to wait for */
function attemptFlush(chatId, interactive) {
  const cs = chatState(chatId);
  if (!cs.queue?.length || cs.running || cs._dispatching) return;
  const ep = activeEndpoint(cs);
  if (!ep.provider) {
    if (interactive) toast("No providers defined in loom.yaml yet.", "warn");
    return;
  }
  dispatchMessage(chatId, cs.queue[0]);   // peek - removed on success
}

/* called on every providers event: refresh the queue chrome */
function chatsOnProvidersEvent() {
  for (const tab of st.tabs) {
    if (tab.type !== "chat") continue;
    const cs = st.chats[tab.chatId];
    if (cs?.queue?.length) renderQueue(tab.chatId);
  }
}

/* ---------- the chat tools bar ----------
 * A strip across the top of every chat for manipulating the SIGNALS the
 * model receives. Time travel is the first tool; the spacer leaves room
 * for more.
 *
 * TIME TRAVEL: every user message carries its UTC send time in
 * [brackets] on the wire. With an offset armed, each NEW message is
 * stamped real-time + offset instead (persisted as `signalTs` at send) -
 * a probe of the model's signal awareness. Real timestamps, the UI, and
 * already-sent stamps stay untouched; clearing returns new messages to
 * real time. */
const TT_MAX_MS = 3650 * 24 * 3600 * 1000;   // ±10y - matches the backend
const TT_SLIDER_N = 1000;
const TT_SPANS = { y: 31536000, w: 604800, d: 86400, h: 3600, m: 60, s: 1 };

/* cubic slider scale: minutes near the middle, years at the ends */
function ttOffsetFromSlider(v) {
  const f = Math.abs(v) / TT_SLIDER_N;
  return Math.round(Math.sign(v) * f * f * f * TT_MAX_MS);
}
function ttSliderFromOffset(ms) {
  const f = Math.cbrt(Math.min(1, Math.abs(ms) / TT_MAX_MS));
  return Math.round(Math.sign(ms) * f * TT_SLIDER_N);
}

function fmtTTOffset(ms) {
  if (!ms) return "off";
  const sign = ms < 0 ? "-" : "+";
  let s = Math.round(Math.abs(ms) / 1000);
  const parts = [];
  for (const [u, span] of [["y", TT_SPANS.y], ["d", TT_SPANS.d],
    ["h", TT_SPANS.h], ["m", TT_SPANS.m], ["s", TT_SPANS.s]]) {
    const n = Math.floor(s / span);
    if (n) { parts.push(n + u); s -= n * span; }
  }
  return sign + (parts.length ? parts.join(" ") : "0s");
}

/* "+1y 2d 5m 3s" / "-3h 30m" → ms; "" / "off" / "0" → 0; garbage → null */
function parseTTOffset(text) {
  const t = String(text || "").trim();
  if (!t || t === "+" || /^(off|0)$/i.test(t)) return 0;
  const sign = t.startsWith("-") ? -1 : 1;
  const body = t.replace(/^[+-]/, "");
  let secs = 0, matched = 0;
  const re = /(\d+)\s*([ywdhms])/gi;
  let m;
  while ((m = re.exec(body))) {
    secs += Number(m[1]) * TT_SPANS[m[2].toLowerCase()];
    matched++;
  }
  return matched ? sign * secs * 1000 : null;
}

/* what a message sent right now would report */
function ttStamp(offsetMs) {
  return new Date(Date.now() + offsetMs).toISOString()
    .slice(0, 16).replace("T", " ") + " UTC";
}

function renderChatToolsbar(chatId) {
  const panel = chatPanel(chatId);
  const bar = panel?.querySelector('[data-role="toolsbar"]');
  if (!bar) return;
  const cs = chatState(chatId);
  const off = Number(cs.chat?.timeTravelMs || 0);
  const sigs = !cs.chat?.timeSignalsOff;
  bar.replaceChildren();

  const state = !sigs ? "signals off" : off ? fmtTTOffset(off) : "";
  const btn = el("button", {
    class: "perm-pill ttrav-btn" + (!sigs || off ? " on" : ""),
    "data-role": "ttrav",
    title: "Datetime signals: what time the model is told, and time "
      + "travel to shift it - click for the panel",
  },
    el("span", { class: "ttrav-lbl", html: icon("clock", 13) }),
    el("span", { text: "time travel" + (state ? " · " + state : "") }));
  btn.addEventListener("click", () => timeTravelMenu(btn, chatId));
  bar.append(btn);
  if (off && sigs) {
    bar.append(el("button", { class: "msg-act ttrav-clear", text: "×",
      title: "Clear time travel - new messages report real time again",
      onclick: () => setTimeTravel(chatId, 0) }));
  }

  const pill = (role, ic, label, cls, title, onClick) => {
    const b = el("button", {
      class: "perm-pill ttrav-btn" + cls, "data-role": role, title,
    },
      el("span", { class: "ttrav-lbl", html: icon(ic, 13) }),
      el("span", { text: label }));
    b.addEventListener("click", onClick);
    bar.append(b);
    return b;
  };

  // knowledge base cut: tool, mounts, and prompt mentions all go
  const kbOn = !cs.chat?.knowledgeOff;
  pill("kb", "library", kbOn ? "knowledge" : "knowledge · off",
    kbOn ? "" : " off-warn",
    kbOn
      ? "The knowledge base is available to the model - click to cut it "
        + "off for this chat (the search tool, the /knowledge mount, and "
        + "every prompt mention all go)"
      : "The knowledge base is CUT OFF for this chat - click to restore",
    async () => {
      const r = await Api.call("chat_set_knowledge", chatId, !kbOn);
      if (!r.ok) { toast(r.error, "err"); return; }
      if (r.data.knowledge) delete cs.chat.knowledgeOff;
      else cs.chat.knowledgeOff = true;
      renderChatToolsbar(chatId);
      refreshChatSilently(chatId);   // the context estimate moves too
    });

  // network: a popout with the three modes
  const net = netMode(cs.chat?.network);
  setHotkey(pill("net", "globe",
    "network · " + (net === "on" ? "on"
      : net === "loopback" ? "loopback" : "none"),
    net === "on" ? " off-warn" : net === "loopback" ? " on" : "",
    "Container network for shell commands - click for the options  (Ctrl+/)",
    (e) => networkMenu(e.currentTarget, chatId)), "Ctrl+/");

  // MCP tools: per-chat permission overrides
  const nOver = Object.keys(cs.chat?.mcpPerms || {}).length;
  pill("mcptools", "mcp",
    "mcp tools" + (nOver ? " · " + nOver + " set" : ""),
    nOver ? " on" : "",
    "Per-tool MCP permissions for THIS chat - overrides loom.yaml modes "
      + "and the MCP tab's defaults",
    (e) => mcpToolsMenu(e.currentTarget, chatId));

  // artifacts: the on/off cut + everything the model delivered
  const artsOn = !cs.chat?.artifactsOff;
  const dis = new Set(cs.chat?.artifactsDismissed || []);
  const nArts = (cs.chat?.artifacts || [])
    .filter((a) => !dis.has(a.name)).length;
  setHotkey(pill("arts", "box",
    "artifacts" + (!artsOn ? " · off" : nArts ? " · " + nArts : ""),
    !artsOn ? " off-warn" : nArts ? " on" : "",
    "Files the model delivered, and the delivery on/off cut  (Ctrl+[)",
    (e) => artifactsMenu(e.currentTarget, chatId)), "Ctrl+[");

  // env signals: only when an environment is loaded
  if (cs.chat?.env) {
    const nHid = (cs.chat?.envHidden || []).length;
    pill("envsig", "key",
      "env signals" + (nHid ? " · " + nHid + " hidden" : ""),
      nHid ? " on" : "",
      "Per-variable exposure of the '" + cs.chat.env
        + "' environment - hidden variables still load, the model just "
        + "isn't told about them",
      (e) => envSignalsMenu(e.currentTarget, chatId));
  }

  bar.append(el("span", { class: "spacer" }));

  // the mirror terminal: a shell in this chat's exact container setup,
  // popped out into its own window; it restarts to follow setup changes
  const termBtn = el("button", {
    class: "perm-pill ttrav-btn", "data-role": "cterm",
    title: "Open a terminal window in this chat's EXACT container setup "
      + "- same image, mounts, network and environment, the chat's own "
      + "/home/loom - to inspect the environment the way the model sees "
      + "it. The shell restarts to match whenever the chat's setup "
      + "changes.",
  },
    el("span", { class: "ttrav-lbl", html: icon("terminal", 13) }),
    el("span", { text: "terminal" }));
  termBtn.addEventListener("click", async () => {
    const r = await Api.call("chat_term_popout", chatId);
    if (!r.ok) toast(r.error, "err");
  });
  bar.append(termBtn);
}

/* the panel behind the time-travel button: the master signals toggle,
 * the offset slider, and the editable value */
function timeTravelMenu(anchor, chatId) {
  popupMenu(anchor, (menu) => {
    menu.classList.add("ttrav-panel");
    const paint = () => {
      menu.replaceChildren();
      const cs = chatState(chatId);
      const off = Number(cs.chat?.timeTravelMs || 0);
      const sigs = !cs.chat?.timeSignalsOff;

      // master switch: signals OFF hides EVERY datetime from the model
      const tog = el("button", {
        class: "btn btn-sm" + (sigs ? " btn-acc" : " btn-danger"),
        text: sigs ? "on" : "off",
        title: sigs
          ? "Click to hide every datetime signal from the model - no "
            + "session stamp, no message time brackets"
          : "Click to expose datetime signals to the model again",
      });
      tog.addEventListener("click", async () => {
        const r = await Api.call("chat_set_time_signals", chatId, !sigs);
        if (!r.ok) { toast(r.error, "err"); return; }
        if (r.data.timeSignals) delete cs.chat.timeSignalsOff;
        else cs.chat.timeSignalsOff = true;
        renderChatToolsbar(chatId);
        paint();
      });
      menu.append(el("div", { class: "ttrav-row" },
        el("span", { class: "ttrav-name", text: "datetime signals" }),
        el("span", { class: "spacer" }), tog));

      const val = el("span", { class: "ttrav-val", text: fmtTTOffset(off),
        title: "Double-click to type an offset like +1y 2d 5m 3s" });
      const slider = el("input", { type: "range", class: "ttrav-slider",
        min: String(-TT_SLIDER_N), max: String(TT_SLIDER_N), step: "1",
        value: String(ttSliderFromOffset(off)),
        title: "Drag to shift reported time - fine steps near the "
          + "middle, years at the ends; release to arm" });
      if (!sigs) { slider.disabled = true; val.classList.add("dim"); }
      slider.addEventListener("input", () => {
        val.textContent = fmtTTOffset(ttOffsetFromSlider(Number(slider.value)));
      });
      slider.addEventListener("change", () =>
        setTimeTravel(chatId, ttOffsetFromSlider(Number(slider.value)))
          .then(paint));
      if (sigs) {
        val.addEventListener("dblclick", () => editTTValue(chatId, val, paint));
      }
      const row = el("div", { class: "ttrav-row" }, slider, val);
      if (off && sigs) {
        row.append(el("button", { class: "msg-act ttrav-clear", text: "×",
          title: "Clear time travel",
          onclick: () => setTimeTravel(chatId, 0).then(paint) }));
      }
      menu.append(row);

      menu.append(el("div", { class: "ttrav-note",
        text: !sigs
          ? "no datetime signals reach the model"
          : off ? "new messages report: " + ttStamp(off)
                : "new messages report real time" }));
    };
    paint();
  });
}

/* ---------- network: the tools-bar popout ---------- */
function networkMenu(anchor, chatId) {
  popupMenu(anchor, (menu, close) => {
    menu.classList.add("ttrav-panel");
    const cs = chatState(chatId);
    const cur = netMode(cs.chat?.network);
    const opt = (mode, label, desc) => {
      const row = el("div", {
        class: "ctx-item" + (mode === cur ? " sel" : "") },
        el("span", { class: "netopt-lbl", text: label }),
        el("span", { class: "netopt-desc", text: desc }));
      row.addEventListener("click", async () => {
        const r = await Api.call("chat_set_network", chatId, mode);
        if (!r.ok) { toast(r.error, "err"); return; }
        cs.chat.network = r.data.network;
        renderChatToolsbar(chatId);
        refreshChatSilently(chatId);   // the prompt's network note changed
        close();
      });
      menu.append(row);
    };
    menu.append(el("div", { class: "ttrav-name",
      text: "container network" }));
    opt("none", "no network",
      "--network=none; the container's own loopback still works");
    opt("loopback", "loopback only",
      "host 127.0.0.1 services at 10.0.2.2; no internet (podman)");
    opt("on", "network on", "the engine's default network");
  });
}

/* ---------- MCP tools: per-chat permission overrides ---------- */
function mcpToolsMenu(anchor, chatId) {
  popupMenu(anchor, (menu) => {
    menu.classList.add("ttrav-panel");
    menu.append(el("div", { class: "ttrav-name", text: "mcp tools - this chat" }),
      el("div", { class: "ttrav-note", text: "loading…" }));
    Api.call("mcp_status").then((r) => {
      menu.querySelector(".ttrav-note")?.remove();
      if (!r.ok) {
        menu.append(el("div", { class: "ttrav-note", text: r.error }));
        return;
      }
      const cs = chatState(chatId);
      const rows = (r.data.servers || [])
        .flatMap((s) => s.tools || []);
      if (!rows.length) {
        menu.append(el("div", { class: "ttrav-note",
          text: "no MCP tools running - enable servers in the MCP "
            + "Servers tab" }));
        return;
      }
      menu.append(el("div", { class: "ttrav-note",
        text: "an override here beats loom.yaml modes and the MCP tab's "
          + "defaults - 'default' hands the decision back" }));
      for (const t of rows) {
        const sel = el("select", { class: "term-sel mcp-perm-sel" });
        sel.append(el("option", { value: "",
          text: "default (" + t.perm + ")" }));
        for (const lv of ["allow", "ask", "deny", "disabled"]) {
          sel.append(el("option", { value: lv, text: lv }));
        }
        sel.value = (cs.chat?.mcpPerms || {})[t.fullName] || "";
        sel.addEventListener("change", async () => {
          const res = await Api.call("chat_set_mcp_perm", chatId,
            t.fullName, sel.value);
          if (!res.ok) { toast(res.error, "err"); return; }
          if (Object.keys(res.data.mcpPerms).length) {
            cs.chat.mcpPerms = res.data.mcpPerms;
          } else {
            delete cs.chat.mcpPerms;
          }
          renderChatToolsbar(chatId);
          refreshChatSilently(chatId);   // tool specs move
        });
        // ctx-item = arrow-navigable; Enter lands focus on the select,
        // whose own arrows then cycle the levels
        const row = el("div", { class: "ctx-item ttrav-row",
          onclick: (e) => { if (e.target !== sel) sel.focus(); } },
          el("span", { class: "mcp-fn", text: t.fullName,
            title: t.description || "" }),
          el("span", { class: "spacer" }), sel);
        menu.append(row);
      }
    });
  });
}

/* ---------- artifacts: the on/off cut + delivered files ---------- */
function artifactsMenu(anchor, chatId) {
  popupMenu(anchor, (menu) => {
    menu.classList.add("ttrav-panel");
    const paint = () => {
      menu.replaceChildren();
      const cs = chatState(chatId);
      const on = !cs.chat?.artifactsOff;
      const tog = el("button", {
        class: "btn btn-sm" + (on ? " btn-acc" : " btn-danger"),
        text: on ? "on" : "off",
        title: on
          ? "Click to disable artifact delivery for this chat - the "
            + "deliver_artifact tool goes away; no new deliveries"
          : "Click to enable artifact delivery again",
      });
      tog.addEventListener("click", async () => {
        const r = await Api.call("chat_set_artifacts", chatId, !on);
        if (!r.ok) { toast(r.error, "err"); return; }
        if (r.data.artifacts) delete cs.chat.artifactsOff;
        else cs.chat.artifactsOff = true;
        renderChatToolsbar(chatId);
        refreshChatSilently(chatId);
        paint();
      });
      menu.append(el("div", { class: "ttrav-row" },
        el("span", { class: "ttrav-name", text: "artifacts" }),
        el("span", { class: "spacer" }), tog));
      const dismissed = new Set(cs.chat?.artifactsDismissed || []);
      const arts = (cs.chat?.artifacts || [])
        .filter((a) => !dismissed.has(a.name));
      if (!arts.length) {
        menu.append(el("div", { class: "ttrav-note",
          text: on ? "nothing delivered yet" : "deliveries are disabled" }));
        return;
      }
      for (const a of arts) {
        const isImg = !a.dir && /\.(png|jpe?g|webp|gif|bmp)$/i.test(a.name);
        // ctx-item = arrow-navigable; Enter (the row's click) opens it
        menu.append(el("div", { class: "ctx-item ttrav-row",
          onclick: () => Api.call("artifact_open", chatId, a.name) },
          el("span", { html: icon(a.dir ? "box" : isImg ? "image" : "file", 12) }),
          el("span", { class: "mcp-fn", text: a.name,
            title: (a.dir ? "folder - saves as a zip" : "file") + " · "
              + fmtBytes(a.bytes) + " · click to open" }),
          el("span", { class: "spacer" }),
          el("button", { class: "btn btn-sm", text: a.dir ? "zip" : "save",
            title: a.dir ? "Save this folder as a zip…" : "Save this file…",
            onclick: async (e) => {
              e.stopPropagation();
              const res = await Api.call("artifact_save", chatId, a.name);
              if (!res.ok) { toast(res.error, "err"); return; }
              if (res.data.saved) toast("Saved " + res.data.saved, "ok");
            } }),
          el("button", { class: "msg-act ttrav-clear", text: "×",
            title: "Dismiss - the file and its timeline entry stay",
            onclick: async (e) => {
              e.stopPropagation();
              const res = await Api.call("artifact_dismiss", chatId, a.name);
              if (!res.ok) { toast(res.error, "err"); return; }
              cs.chat.artifactsDismissed = res.data.dismissed;
              renderChatToolsbar(chatId);
              paint();
            } })));
      }
    };
    paint();
  });
}

/* ---------- env signals: per-variable exposure ---------- */
function envSignalsMenu(anchor, chatId) {
  popupMenu(anchor, (menu) => {
    menu.classList.add("ttrav-panel");
    const cs = chatState(chatId);
    const envName = cs.chat?.env || "";
    menu.append(
      el("div", { class: "ttrav-name", text: "env signals · " + envName }),
      el("div", { class: "ttrav-note", text: "loading…" }));
    Api.call("env_get", envName).then((r) => {
      menu.querySelector(".ttrav-note")?.remove();
      if (!r.ok) {
        menu.append(el("div", { class: "ttrav-note", text: r.error }));
        return;
      }
      menu.append(el("div", { class: "ttrav-note",
        text: "unchecked variables still LOAD into shell containers - "
          + "the model just isn't told they exist" }));
      const hidden = new Set(cs.chat?.envHidden || []);
      const vars = r.data.vars || [];
      for (const v of vars) {
        const ck = el("input", { type: "checkbox" });
        ck.checked = !hidden.has(v.key);
        ck.addEventListener("change", async () => {
          if (ck.checked) hidden.delete(v.key);
          else hidden.add(v.key);
          const res = await Api.call("chat_set_env_hidden", chatId,
            [...hidden]);
          if (!res.ok) { toast(res.error, "err"); return; }
          if (res.data.envHidden.length) {
            cs.chat.envHidden = res.data.envHidden;
          } else {
            delete cs.chat.envHidden;
          }
          renderChatToolsbar(chatId);
          refreshChatSilently(chatId);   // the env prompt line changed
        });
        // ctx-item = arrow-navigable; Enter clicks the label, which
        // toggles its checkbox
        menu.append(el("label", { class: "ctx-item ttrav-row chk" }, ck,
          el("span", { class: "mcp-fn",
            text: v.key + (v.secret ? "  (secret)" : "") })));
      }
      if (!vars.length) {
        menu.append(el("div", { class: "ttrav-note",
          text: "this environment has no variables" }));
      }
    });
  });
}

/* double-click the value: edit it as text, sign included */
function editTTValue(chatId, val, rerender) {
  const cs = chatState(chatId);
  const input = el("input", { type: "text", class: "ttrav-edit",
    value: cs.chat?.timeTravelMs ? fmtTTOffset(cs.chat.timeTravelMs) : "+",
    title: "Units: y w d h m s (e.g. +1y 2d 5m 3s); 'off' or empty clears" });
  val.replaceWith(input);
  input.focus();
  input.select();
  let closed = false;
  const done = (commit) => {
    if (closed) return;
    closed = true;
    if (commit) {
      const ms = parseTTOffset(input.value);
      if (ms === null) {
        toast("Could not read that - use e.g. +1y 2d 5m 3s", "warn");
      } else if (Math.abs(ms) > TT_MAX_MS) {
        toast("Keep time travel within ±10 years.", "warn");
      } else {
        setTimeTravel(chatId, ms).then(() => rerender());
        return;
      }
    }
    rerender();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); done(true); }
    else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      done(false);
    }
  });
  input.addEventListener("blur", () => done(false));
}

async function setTimeTravel(chatId, offsetMs) {
  const r = await Api.call("chat_set_time_travel", chatId, offsetMs);
  if (!r.ok) {
    toast(r.error, "err");
    renderChatToolsbar(chatId);
    return;
  }
  const cs = chatState(chatId);
  if (cs.chat) {
    if (r.data.offsetMs) cs.chat.timeTravelMs = r.data.offsetMs;
    else delete cs.chat.timeTravelMs;
  }
  renderChatToolsbar(chatId);
  toast(r.data.offsetMs
    ? "Time travel armed - new messages report " + fmtTTOffset(r.data.offsetMs)
      + " (" + ttStamp(r.data.offsetMs) + " right now)."
    : "Time travel cleared - new messages report real time.", "ok", 3000);
}

/* ---------- auto-continue (Ctrl+Shift+R) ----------
 * A per-chat toggle: after every completed response the chat continues
 * itself with no new input - the model just keeps generating. The bar
 * above the composer is the always-on signal while it's armed. It
 * disarms itself the moment anything interrupts: Esc/Stop, an error, or
 * the loop giving up on empty responses. */
function toggleAutoContinue(chatId) {
  const cs = chatState(chatId);
  cs.autoContinue = !cs.autoContinue;
  renderAutoGen(chatId);
  if (cs.autoContinue) {
    toast("Auto-continue ON - the model keeps generating after each "
      + "response. Ctrl+Shift+R (or the bar's button) turns it off.",
      "ok", 3500);
    // an idle chat starts right away - an empty one has the model
    // OPEN the conversation
    if (!cs.running && !cs.live && !cs.compacting && !cs.queue?.length) {
      continueChat(chatId);
    }
  } else {
    toast("Auto-continue off", "ok", 1800);
  }
}

function renderAutoGen(chatId) {
  const panel = chatPanel(chatId);
  const host = panel?.querySelector('[data-role="autogen"]');
  if (!host) return;
  const cs = chatState(chatId);
  host.replaceChildren();
  host.classList.toggle("on", !!cs.autoContinue);
  if (!cs.autoContinue) return;
  const off = el("button", { class: "btn btn-sm", text: "Turn off",
    title: "Stop auto-continuing  (Ctrl+Shift+R)" });
  off.addEventListener("click", () => toggleAutoContinue(chatId));
  host.append(
    el("span", { class: "autogen-dot" }),
    el("span", { class: "autogen-text",
      text: "Auto-continue is ON - after each response the model keeps "
        + "generating with no new input." }),
    el("span", { class: "spacer" }),
    off);
}

/* the done-event hook: keep going unless something called it off */
function maybeAutoContinue(chatId, ev) {
  const cs = chatState(chatId);
  if (!cs.autoContinue) return;
  if (ev.cancelled || ev.gaveUp) {
    cs.autoContinue = false;
    renderAutoGen(chatId);
    toast("Auto-continue off - " + (ev.cancelled
      ? "generation was stopped."
      : "the model gave up on an empty response."), "warn", 3500);
    return;
  }
  // a beat of delay: queued user messages flush first, and Esc still
  // has a moment to land between turns
  setTimeout(() => {
    const s = chatState(chatId);
    if (!s.autoContinue || s.running || s.live || s.compacting) return;
    if (s.queue?.length) return;
    continueChat(chatId, false);   // automatic: never steals the scroll
  }, 500);
}

/* Ctrl+R, whatever the ending: the Retry/Continue banner's action when
 * one is showing (user/tool ending, stopped response); on a chat whose
 * last word is a FINISHED model answer, a bare "continue with no new
 * input"; and on an EMPTY chat, the model produces the FIRST message -
 * the loop runs on the system prompt alone and it opens the
 * conversation. */
function resumeChat(chatId) {
  const cs = st.chats[chatId];
  if (!cs?.chat || cs.running || cs.live || cs.compacting) return;
  continueChat(chatId);
}

async function continueChat(chatId, armFollow = true) {
  const cs = chatState(chatId);
  if (cs.running) return;
  cs.error = null;
  const res = await Api.call("chat_continue", chatId);
  if (!res.ok) { toast(res.error, "err"); return; }
  if (armFollow) {
    // Ctrl+R / the Retry-Continue banner is the same intent as sending:
    // "generate - I want to watch". Only AUTOMATIC continuations
    // (auto-continue's loop) skip this, so they never hijack a reader.
    cs.follow = true;
    cs.followTail = false;
    cs.atBottom = true;
  }
  cs.running = true;
  cs.respT0 = performance.now();
  cs.churn = null;
  cs.retryNote = null;
  cs.turnT0 = performance.now();
  cs.live = { text: "", think: "" };
  cs.progress = null;
  cs.liveTimings = null;
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
  // SENDING RE-ARMS AUTO-SCROLL: pressing Enter is an explicit "I want
  // to see this go out and the reply come in" - the one deterministic
  // exception to "only scrolling changes the follow flag". The reply
  // clamps at its start (read-from-the-beginning), not the raw tail.
  cs.follow = true;
  cs.followTail = false;
  cs.atBottom = true;
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
    // engage only from the very start of an input (or an empty one) -
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

/* The retry banner announces a PENDING recovery ("the model stopped
 * without answering - passing the turn back"). The instant the retried
 * turn produces anything real - streamed text, thinking, a tool call -
 * the recovery succeeded and the banner is a lie; drop it. Without
 * this it sat on screen through whole tool runs. */
function _clearRetryNote(chatId, cs) {
  if (!cs.retryNote) return;
  cs.retryNote = null;
  queueThreadRedraw(chatId);
}

function onChatEvent(ev) {
  const chatId = ev.chatId;
  const cs = chatState(chatId);
  // after an eager cancel everything in-flight is slop being discarded -
  // only the terminal events (done/error) end the discard window
  if (cs.discarding && !["done", "error", "title", "compact_done",
                         "compact_error", "compact_cancelled"].includes(ev.kind)) {
    return;
  }
  switch (ev.kind) {
    case "start":
      cs.running = true;
      cs.retryNote = null;
      cs.turnT0 = performance.now();   // the prefill clock
      cs.live = cs.live || { text: "", think: "" };
      cs.followTail = false;   // each fresh reply re-clamps at its start
      renderSendButton(chatId);
      renderTabs();
      break;
    case "delta": {
      _clearRetryNote(chatId, cs);
      cs.live = cs.live || { text: "", think: "" };
      cs.live.text += ev.text;
      // fast path: patch the live body in place when it's on screen -
      // always through liveVisibleText, so an echoed signal stamp never
      // flashes up mid-stream
      const panel = chatPanel(chatId);
      const liveEl = panel?.querySelector('[data-role="live"]');
      if (liveEl && !selectionWithin(liveEl)) {
        const thread = panel.querySelector('[data-role="thread"]');
        const visText = liveVisibleText(cs.live.text);
        liveEl.innerHTML = renderMarkdown(visText);
        if (visText.length <= ENHANCE_LIVE_MAX) enhanceCodeBlocks(liveEl);
        liveEl.append(el("span", { class: "cursor" }));
        tickLiveCount(chatId);
        if (chatFollows(cs)) followScroll(thread, cs);
        if (panel._updateJump) panel._updateJump();
      } else if (!liveEl) {
        queueThreadRedraw(chatId);
      }
      // a selection inside the live element: text keeps accumulating in
      // cs.live and lands on the next unobstructed patch
      break;
    }
    case "think": {
      _clearRetryNote(chatId, cs);
      cs.live = cs.live || { text: "", think: "" };
      cs.live.think += ev.text;
      // fast path mirrors delta: patch the live think block in place -
      // full-thread redraws during streaming caused visible churn
      const panel = chatPanel(chatId);
      const tEl = panel?.querySelector('[data-role="live-think"]');
      if (tEl && tEl.firstChild && !selectionWithin(tEl)) {
        const thread = panel.querySelector('[data-role="thread"]');
        // update the TEXT NODE only - the cursor sibling lives inside
        // the thought while it streams and must survive deltas
        tEl.firstChild.nodeValue = cs.live.think;
        tickLiveCount(chatId);   // thinking counts toward liveness too
        if (chatFollows(cs)) followScroll(thread, cs);
        if (panel._updateJump) panel._updateJump();
      } else if (!tEl) {
        queueThreadRedraw(chatId);
      }
      break;
    }
    case "tool_begin":
      _clearRetryNote(chatId, cs);
      cs.tools[ev.callId] = { tool: ev.tool, state: "announced" };
      cs.followTail = false;   // the NEXT turn's reply re-clamps too
      queueThreadRedraw(chatId);
      break;
    case "tool_call":
      _clearRetryNote(chatId, cs);
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
      cs.progress = null;                  // its prefill starts fresh
      cs.liveTimings = null;
      cs.turnT0 = performance.now();       // its prefill clock restarts
      refreshChatSilently(chatId);
      break;
    case "notice":
      // the worker flagged something the user must know (a truncated
      // reply, unparsed tool-call markup) - visible, never silent
      toast(ev.msg, "warn", 6500);
      break;
    case "retry":
      // the turn ended without an answer (a thought that halted, an
      // empty stream) - the loop is passing it back to the model
      cs.retryNote = "The model stopped without answering - passing the "
        + "turn back (attempt " + ev.attempt + "/" + ev.max + ")";
      cs.live = { text: "", think: "" };
      cs.progress = null;
      cs.liveTimings = null;
      cs.turnT0 = performance.now();
      refreshChatSilently(chatId);
      break;
    case "stats": {
      // attach to the last assistant message once refreshed
      cs.lastStats = { timings: ev.timings, usage: ev.usage, ttftMs: ev.ttftMs };
      break;
    }
    case "live_stats":
      // per-chunk timings snapshot - the tok/s beside the cursor
      cs.liveTimings = ev.timings;
      tickLiveCount(chatId);
      break;
    case "progress":
      // the server is reading the prompt: {total, cache, processed, time_ms}
      cs.progress = ev.progress;
      tickLiveCount(chatId);
      break;
    case "title":
      if (cs.chat) cs.chat.title = ev.title;
      renderTabs();
      refreshArchiveTab();
      break;
    case "artifacts":
      // the model delivered an artifact - surface it immediately
      if (cs.chat) cs.chat.artifacts = ev.items || [];
      renderAttachBar(chatId);
      if (ev.fresh?.length) {
        toast("Artifact" + (ev.fresh.length > 1 ? "s" : "") + " from the "
          + "model: " + ev.fresh.join(", "), "ok");
      }
      break;
    case "compact_start":
      _clearRetryNote(chatId, cs);   // something new took over the chat
      cs.compacting = true;
      cs.compactTok = 0;
      queueThreadRedraw(chatId);
      renderSendButton(chatId);   // stop button + Esc work during compaction
      break;
    case "compact_tick": {
      // the rising number IS the health indicator: frozen = stalled
      cs.compacting = true;
      cs.compactTok = ev.tokens || 0;
      const tok = chatPanel(chatId)?.querySelector('[data-role="compact-tok"]');
      if (tok) {
        tok.textContent = "Compacting context… ~"
          + fmtTok(cs.compactTok) + " tok summarized ";
      } else {
        queueThreadRedraw(chatId);
      }
      break;
    }
    case "compact_done":
      cs.compacting = false;
      cs.compactTok = 0;
      cs.discarding = false;
      toast("Context compacted - " + (ev.replaced || 0)
        + " earlier messages summarized.", "ok");
      refreshChatSilently(chatId);
      renderSendButton(chatId);
      break;
    case "compact_cancelled":
      // the user's own Esc/✕ - quiet, not an error
      cs.compacting = false;
      cs.compactTok = 0;
      cs.discarding = false;
      queueThreadRedraw(chatId);
      renderSendButton(chatId);
      break;
    case "compact_error":
      cs.compacting = false;
      cs.compactTok = 0;
      cs.discarding = false;
      toast(ev.msg || "compaction failed", "err");
      queueThreadRedraw(chatId);
      renderSendButton(chatId);
      break;
    case "done":
      cs.churn = mkChurn(cs, ev.gaveUp ? "fail" : "done") || cs.churn;
      cs.retryNote = null;
      cs.discarding = false;
      cs.running = false;
      cs.live = null;
      cs.progress = null;
      cs.liveTimings = null;
      cs.tools = {};
      refreshChatSilently(chatId);
      renderSendButton(chatId);
      renderTabs();
      attemptFlush(chatId, false);   // next queued message goes out
      maybeAutoContinue(chatId, ev);
      break;
    case "error":
      if (cs.autoContinue) {
        // never hammer a failing provider - disarm loudly
        cs.autoContinue = false;
        renderAutoGen(chatId);
        toast("Auto-continue off - the chat hit an error.", "warn", 3500);
      }
      cs.churn = mkChurn(cs, "done") || cs.churn;
      cs.retryNote = null;
      cs.discarding = false;
      cs.running = false;
      cs.live = null;
      cs.progress = null;
      cs.liveTimings = null;
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
  setMsgBase(cs);
  if (cs.lastStats) {
    const last = [...cs.chat.messages].reverse().find((m) => m.role === "assistant");
    if (last) Object.assign(last, cs.lastStats);
    cs.lastStats = null;
  }
  renderCtxChip(chatId);
  renderAttachBar(chatId);   // artifacts ride on the chat doc
  renderChatToolsbar(chatId);   // time travel rides on it too
  queueThreadRedraw(chatId);
  // an open diagnostics view (tab or popped-out window) follows along
  if (typeof refreshDiagView === "function") refreshDiagView(chatId);
}
