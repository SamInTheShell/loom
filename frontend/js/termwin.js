/* termwin.js - bootstrap for the chat-mirror terminal window.
 *
 * The page (termwin.html) hosts ONE chat's diagnostic terminal in a real
 * OS window: a live shell in the exact container setup the chat's model
 * gets - image, /mnt mounts, the /knowledge cut, /uploads, the
 * chat's own /home/loom, network mode and environment. The setup is
 * computed by the BACKEND from the chat document (chat_term_open); this
 * page never copies chat state, so it cannot drift. When the chat's
 * setup changes, the backend restarts the shell to match (scrollback
 * carried) and the header re-reads chat_term_info on the ready event.
 *
 * Loads BEFORE terminal.js: it defines the `st` global and the main-app
 * stubs terminal.js touches, then boot() rebinds openTermSession so
 * every (re)start goes through chat_term_open. Lifecycle is owned by
 * the backend: closing this window kills the shell, and the window
 * closes whenever the main window does. */
"use strict";

/* terminal.js reads these at load time; the child needs only stubs */
const st = { terms: {}, _termViews: {}, tabs: [], config: null };
function saveSession() { /* the child has no session of its own */ }
function renderTabs() { /* no tab strip here */ }
function tabById() {
  // resyncTerms treats the mirror as the active terminal tab, so
  // hidden-window recovery (reattach after tray/minimize) just works
  const cid = window.LOOM_TERM_CHAT;
  return cid ? { type: "term", chatId: "chat-" + cid } : null;
}

window.LOOM_TERM_CHAT = (() => {
  const m = /[#&]chat=([\w-]+)/.exec(location.hash || "");
  return m ? m[1] : "";
})();

let _twBooted = false;

function termWinHeader(info, sid) {
  const roPill = (cls, title, ...kids) =>
    el("span", { class: "pill" + (cls ? " " + cls : ""), title }, ...kids);
  const head = el("div", { class: "term-head" },
    roPill("", "container: " + info.container,
      el("span", { html: icon("servers", 12) }),
      el("span", { class: "pname", text: info.container })));
  for (const f of info.folders || []) {
    head.append(roPill("", f.path,
      el("span", { html: icon("folder", 12) }),
      el("span", { class: "pname", text: "/mnt/" + baseName(f.path) }),
      el("span", { class: "mode" + (f.mode === "write" ? " write" : ""),
                   text: f.mode })));
  }
  head.append(roPill("", info.knowledge
      ? "The library knowledge base, read-only - exactly what the "
        + "chat's shell tools see"
      : "The knowledge base is CUT OFF in this chat - not mounted",
    el("span", { html: icon("library", 12) }),
    el("span", { class: "pname",
                 text: info.knowledge ? "/knowledge" : "knowledge off" })));
  if (info.uploads) {
    head.append(roPill("",
      "The user's uploaded files for this chat, read-only",
      el("span", { html: icon("box", 12) }),
      el("span", { class: "pname", text: "/uploads" })));
  }
  const net = info.network;   // already canonical: none / loopback / on
  head.append(el("span", {
    class: "pill term-net" + (net === "on" ? " on"
      : net === "loopback" ? " loop" : ""),
    text: NET_LABELS[net],
    title: "The chat's network mode - change it on the chat's tools bar",
  }));
  head.append(el("span", { html: icon("key", 12), class: "term-envkey" }),
    el("span", { class: "pill", text: info.env || "no env",
      title: info.env
        ? "Environment '" + info.env + "' loads into the shell - ALL of "
          + "its variables, hidden-from-the-model ones included"
        : "No environment is loaded in this chat" }));
  head.append(el("span", { class: "spacer" }),
    el("span", { class: "ts-hint",
      text: "mirrors the chat's setup - restarts on change" }),
    el("button", {
      class: "btn btn-sm", text: "Restart",
      title: "Restart the shell in the chat's current setup",
      onclick: async () => {
        const v = st._termViews[sid];
        if (!v || v.opening) return;
        let procs = [];
        try {
          const res = await Api.call("term_procs", sid);
          procs = res.ok ? res.data.procs || [] : [];
        } catch (e) { /* backend gone */ }
        const go = () => {
          v.screen.feed("\r\n\x1b[2m[loom] restarting shell…\x1b[0m\r\n");
          v.opening = false;
          openTermSession(sid, v);
        };
        if (!procs.length) { go(); return; }
        modal("Programs still running",
          [el("p", { text: "Restarting kills:" }),
           el("div", { class: "tool-args", text: procs.join("\n") })],
          [{ label: "Keep running" },
           { label: "Restart anyway", cls: "btn-danger", fn: go }],
          { id: "termwin-restart" });
      },
    }));
  return head;
}

async function termWinRefreshHeader() {
  const cid = window.LOOM_TERM_CHAT;
  const root = $("#termroot");
  if (!cid || !root) return;
  const sid = "chat-" + cid;
  try {
    const r = await Api.call("chat_term_info", cid);
    if (!r.ok) return;
    const head = termWinHeader(r.data, sid);
    const old = root.querySelector(".term-head");
    if (old) old.replaceWith(head);
    else root.prepend(head);
  } catch (e) { /* backend gone - the old header stands */ }
}

async function termWinBoot() {
  if (_twBooted) return;
  _twBooted = true;
  const cid = window.LOOM_TERM_CHAT;
  const root = $("#termroot");
  if (!cid) {
    root.append(el("div", { class: "picker-empty",
      text: "No chat id - open the terminal from a chat's tools bar." }));
    return;
  }
  try {
    const d = await Api.get("app_state");
    document.documentElement.dataset.theme =
      d.theme === "light" ? "light" : "dark";
  } catch (e) { /* dark default stands */ }

  const sid = "chat-" + cid;
  st.activeTab = "term:" + sid;   // the ready handler focuses the view
  st.terms[sid] = { container: "", folders: [], network: "none", env: "",
                    title: null, started: true, running: false };

  // every (re)start goes through the backend's chat-document setup
  openTermSession = (id, v) => {
    if (v.opening) return;
    v.opening = true;
    v.exited = false;
    Api.call("chat_term_open", cid, v.cols, v.rows);
  };

  await termWinRefreshHeader();
  const v = ensureTermView(sid);
  root.append(v.el);
  setTimeout(() => {
    v.measure?.();
    termAttachOrOpen(sid, v);   // reuse a live session, else open fresh
    v.el.focus();
  }, 0);
}

onLMEvent((ev) => {
  const cid = window.LOOM_TERM_CHAT;
  if (!cid || ev.type !== "term" || ev.sid !== "chat-" + cid) return;
  onTermEvent(ev);
  // a restart may carry a NEW setup - the ready event re-reads it
  if (ev.kind === "ready") termWinRefreshHeader();
});

window.addEventListener("pywebviewready", termWinBoot);
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => { if (!window.pywebview) termWinBoot(); }, 1200);
});
