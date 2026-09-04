/* diagwin.js - bootstrap for the popped-out diagnostics window.
 *
 * The page (diagwin.html) hosts ONE chat's diag view in a real OS
 * window. It shares the pywebview bridge api and the event bus with the
 * main window: chat events for this chat trigger the same live refresh
 * the in-app tab gets. The "Return to app" button (diag.js draws it
 * when LOOM_DIAG_CHILD is set) closes this window and reopens the tab.
 * Lifecycle is owned by the backend: this window closes whenever the
 * main window does. */
"use strict";

/* diag.js reads these; the child needs only stubs */
const st = { chats: {}, _diagViews: {} };
function saveSession() { /* the child has no session of its own */ }

window.LOOM_DIAG_CHILD = (() => {
  const m = /[#&]chat=([\w-]+)/.exec(location.hash || "");
  return m ? m[1] : "";
})();

let _dwBooted = false;

async function diagWinBoot() {
  if (_dwBooted) return;
  _dwBooted = true;
  const chatId = window.LOOM_DIAG_CHILD;
  const root = $("#diagroot");
  if (!chatId) {
    root.append(el("div", { class: "picker-empty",
      text: "No chat id - open diagnostics from a chat's context chip." }));
    return;
  }
  // match the app's theme (set once at open)
  try {
    const d = await Api.get("app_state");
    document.documentElement.dataset.theme =
      d.theme === "light" ? "light" : "dark";
  } catch (e) { /* dark default stands */ }
  mountDiagTab(root, chatId);
}

/* the shared event bus: refresh on anything that lands new chat data */
onLMEvent((ev) => {
  const chatId = window.LOOM_DIAG_CHILD;
  if (!chatId) return;
  if (ev.type === "chat" && ev.chatId === chatId
      && ["done", "error", "tool_result", "tool_call", "stats", "title",
          "compact_done"].includes(ev.kind)) {
    refreshDiagView(chatId);
  }
});

window.addEventListener("pywebviewready", diagWinBoot);
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => { if (!window.pywebview) diagWinBoot(); }, 1200);
});
