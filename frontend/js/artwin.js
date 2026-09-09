/* artwin.js - bootstrap for the artifact preview/editor window.
 *
 * One delivered artifact from a chat, in a real OS window:
 *   - text: the markdown/code editor, read-write IN PLACE - Save (or
 *     Ctrl+S) writes straight back to the artifact file, so the model
 *     reads the edited version on its next tool call;
 *   - images: rendered;
 *   - folders and binaries: a download button, nothing pretended.
 * Every kind gets "Save to..." (the app's save dialog). The window
 * shares the bridge api and event bus with the main window and closes
 * with it. If the MODEL rewrites the file while this window is open, a
 * clean editor reloads silently; a dirty one keeps your edits and says
 * so. */
"use strict";

/* util.js reads these when toasting; the child needs only stubs */
const st = { chats: {}, _diagViews: {} };
function saveSession() { /* the child has no session of its own */ }

const ART = (() => {
  const q = {};
  for (const m of (location.hash || "").matchAll(/[#&]([a-z]+)=([^&]*)/g)) {
    q[m[1]] = decodeURIComponent(m[2]);
  }
  return { chat: q.chat || "", name: q.name || "" };
})();

let _artEd = null;      // the LoomEditor, when text
let _artDirty = false;

function artFileUrl(p) {
  return "file://" + encodeURI(String(p)).replace(/#/g, "%23").replace(/\?/g, "%3F");
}

async function artSaveInPlace() {
  if (!_artEd) return;
  const r = await Api.call("artifact_write", ART.chat, ART.name,
    _artEd.getValue());
  if (!r.ok) { toast(r.error, "err"); return; }
  _artEd.markClean();
  _artDirty = false;
  renderArtBar();
  toast("Saved - the model sees this version now.", "ok", 2200);
}

function renderArtBar(info) {
  renderArtBar._info = info = info || renderArtBar._info;
  const bar = $("#artbar");
  if (!bar || !info) return;
  bar.replaceChildren(
    el("b", { text: info.name }),
    el("span", { class: "arc-meta",
      text: (info.kind === "dir" ? "folder"
        : info.kind === "image" ? "image"
        : info.kind === "binary" ? "binary" : "text")
        + (info.bytes ? " · " + fmtBytes(info.bytes) : "") }),
    el("span", { class: "spacer" }),
    info.kind === "text" ? el("button", {
      class: "btn btn-sm" + (_artDirty ? " btn-acc" : ""),
      text: _artDirty ? "Save (Ctrl+S)" : "Saved",
      title: "Write the artifact in place - the model reads the edited "
        + "file on its next tool call",
      onclick: artSaveInPlace,
    }) : null,
    el("button", {
      class: "btn btn-sm", text: "Save to…",
      title: info.kind === "dir" ? "Save this folder as a zip…"
        : "Save a copy where you choose…",
      onclick: async () => {
        const r = await Api.call("artifact_save", ART.chat, ART.name);
        if (!r.ok) { toast(r.error, "err"); return; }
        if (r.data.saved) toast("Saved to " + r.data.saved, "ok");
      },
    }));
}

async function artLoad(silent) {
  const host = $("#arthost");
  const r = await Api.call("artifact_read", ART.chat, ART.name);
  if (!r.ok) {
    host.replaceChildren(el("div", { class: "art-center" },
      el("p", { text: r.error })));
    return;
  }
  const info = r.data;
  document.title = info.name + " · artifact";
  if (info.kind === "text") {
    if (_artEd && silent) {
      // the model rewrote the file: a clean editor just follows
      _artEd.setText(info.text);
      _artEd.markClean();
      _artDirty = false;
      renderArtBar(info);
      return;
    }
    _artEd?.destroy();
    host.replaceChildren();
    const conf = editorModeFor(info.name);
    _artEd = new LoomEditor(host, {
      text: info.text, mode: conf.mode, lang: conf.lang || "",
      onDirty: (d) => { _artDirty = d; renderArtBar(); },
      onSave: artSaveInPlace,
    });
  } else if (info.kind === "image") {
    host.replaceChildren(el("div", { class: "art-center" },
      el("img", { src: artFileUrl(info.path), alt: info.name })));
  } else {
    host.replaceChildren(el("div", { class: "art-center" },
      el("p", { text: info.kind === "dir"
        ? "This artifact is a folder - save it as a zip."
        : "No preview for this file type - download it instead." }),
      el("button", {
        class: "btn btn-acc",
        text: info.kind === "dir" ? "Save as zip…" : "Download…",
        onclick: async () => {
          const rr = await Api.call("artifact_save", ART.chat, ART.name);
          if (!rr.ok) { toast(rr.error, "err"); return; }
          if (rr.data.saved) toast("Saved to " + rr.data.saved, "ok");
        },
      })));
  }
  renderArtBar(info);
}

let _artBooted = false;

async function artBoot() {
  if (_artBooted) return;
  _artBooted = true;
  if (!ART.chat || !ART.name) {
    $("#arthost").replaceChildren(el("div", { class: "art-center" },
      el("p", { text: "No artifact reference - open one from a chat's "
        + "attachment pills." })));
    return;
  }
  try {
    const d = await Api.get("app_state");
    document.documentElement.dataset.theme =
      d.theme === "light" ? "light" : "dark";
  } catch (e) { /* dark default stands */ }
  artLoad(false);
}

/* the shared bus: the model regenerating THIS artifact refreshes a clean
 * view; a dirty editor keeps the user's work and says what happened */
onLMEvent((ev) => {
  if (ev.type !== "chat" || ev.chatId !== ART.chat) return;
  if (ev.kind === "artifacts" && (ev.fresh || []).includes(ART.name)) {
    if (_artEd && _artDirty) {
      toast("The model rewrote this artifact - your unsaved edits are "
        + "kept here; Save overwrites its version.", "warn", 8000);
    } else {
      artLoad(true);
    }
  }
});

window.addEventListener("pywebviewready", artBoot);
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => { if (!window.pywebview) artBoot(); }, 1200);
});
