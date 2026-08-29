/* librarytab.js — the Library tab: a resizable navigation tree on the
 * left (right-click to create/rename/delete), ONE open file on the right
 * (save or discard before navigating away), and fuzzy find-in-files in
 * the search box (file-path matches and content-line matches, ranked). */
"use strict";

function mountLibraryTab(panel) {
  panel.classList.add("libtab");
  const left = el("div", { class: "lib-left" });
  if (st.lib.leftWidth) left.style.width = st.lib.leftWidth + "px";
  const searchWrap = el("div", { class: "lib-search" });
  const searchIn = el("input", {
    type: "text", placeholder: "Search files and content…  (Ctrl+F)",
    spellcheck: "false",
  });
  setHotkey(searchIn, "Ctrl+F");
  searchWrap.append(searchIn);
  const treeHost = el("div", { class: "lib-tree", tabindex: "0" });
  wireLibTreeKeys(treeHost);
  const resHost = el("div", { class: "lib-results hidden" });
  left.append(searchWrap, treeHost, resHost);

  const resizer = el("div", { class: "lib-resizer" });
  setHotkey(resizer, "Ctrl+\\");   // the chip sits right on the split
  const right = el("div", { class: "lib-right" });
  const filebar = el("div", { class: "lib-filebar" });
  const editorHost = el("div", { class: "lib-editor-host", tabindex: "-1" },
    el("div", { class: "lib-placeholder" },
      el("div", { html: icon("library", 40) }),
      el("p", { text: "Select a file from the tree, or right-click it to create one." })));
  right.append(filebar, editorHost);
  panel.append(left, resizer, right);

  st.lib.ui = { panel, treeHost, resHost, filebar, editorHost, searchIn };

  /* resizer drag */
  let dragging = false;
  resizer.addEventListener("mousedown", (e) => {
    dragging = true;
    resizer.classList.add("dragging");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = panel.getBoundingClientRect();
    const w = Math.max(160, Math.min(e.clientX - rect.left, rect.width * 0.6));
    left.style.width = w + "px";
    st.lib.leftWidth = Math.round(w);
  });
  window.addEventListener("mouseup", () => {
    if (dragging) {
      dragging = false;
      resizer.classList.remove("dragging");
      saveSession();
    }
  });

  /* search */
  const doSearch = debounce(async () => {
    const q = searchIn.value.trim();
    st.lib.searching = !!q;
    treeHost.classList.toggle("hidden", !!q);
    resHost.classList.toggle("hidden", !q);
    if (!q) return;
    const res = await Api.call("lib_search", q);
    if (!res.ok) return;
    renderLibResults(res.data.results, q);
  }, 220);
  searchIn.addEventListener("input", doSearch);
  searchIn.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { searchIn.value = ""; doSearch(); }
  });

  /* empty-area context menu */
  treeHost.addEventListener("contextmenu", (e) => {
    if (e.target !== treeHost) return;
    e.preventDefault();
    ctxMenu(e.clientX, e.clientY, rootCtxItems(""));
  });

  loadLibTree();
  renderLibFilebar();
}

async function loadLibTree() {
  const res = await Api.call("lib_tree", !!st.lib.showHidden);
  if (!res.ok) { toast(res.error, "err"); return; }
  st.lib.tree = res.data.tree;
  st.lib.configFile = res.data.configFile;
  // first visit: collapse every top-level folder except documentation/ —
  // the seeded expansion is saved, so it (and every later toggle) is what
  // the library reopens with from then on
  if (st.lib.seedCollapse) {
    st.lib.seedCollapse = false;
    for (const n of st.lib.tree) {
      if (n.dir && n.rel !== "documentation") st.lib.expanded[n.rel] = false;
    }
    saveSession();
  }
  renderLibTree();
}

/* right-click → Collapse all: every folder at every depth */
function libCollapseAll() {
  const walk = (nodes) => {
    for (const n of nodes) {
      if (n.dir) {
        st.lib.expanded[n.rel] = false;
        if (n.children?.length) walk(n.children);
      }
    }
  };
  walk(st.lib.tree || []);
  renderLibTree();
  saveSession();
}

/* ---------- keyboard on the tree ----------
 * The tree is focusable (Ctrl+\ or Tab lands on it); a dashed cursor
 * walks it with the arrows, Enter opens / toggles, Left/Right collapse
 * and expand exactly like every file tree. */
function libVisibleRows() {
  const out = [];
  const walk = (nodes) => {
    for (const n of nodes) {
      out.push(n);
      if (n.dir && st.lib.expanded[n.rel] !== false && n.children?.length) {
        walk(n.children);
      }
    }
  };
  walk(st.lib.tree || []);
  return out;
}

function wireLibTreeKeys(treeHost) {
  treeHost.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const rows = libVisibleRows();
    if (!rows.length) return;
    const idx = rows.findIndex((n) => n.rel === st.lib.cursor);
    const node = idx >= 0 ? rows[idx] : null;
    const setCur = (i) => {
      st.lib.cursor = rows[Math.max(0, Math.min(rows.length - 1, i))].rel;
      renderLibTree();
    };
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCur(idx < 0 ? 0 : idx + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCur(idx < 0 ? 0 : idx - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      setCur(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setCur(rows.length - 1);
    } else if (e.key === "ArrowRight" && node) {
      e.preventDefault();
      if (node.dir && st.lib.expanded[node.rel] === false) {
        st.lib.expanded[node.rel] = true;
        renderLibTree();
        saveSession();
      } else if (node.dir) {
        setCur(idx + 1);   // into the first child
      }
    } else if (e.key === "ArrowLeft" && node) {
      e.preventDefault();
      if (node.dir && st.lib.expanded[node.rel] !== false) {
        st.lib.expanded[node.rel] = false;
        renderLibTree();
        saveSession();
      } else {
        const p = parentRel(node.rel);
        if (p) { st.lib.cursor = p; renderLibTree(); }
      }
    } else if (e.key === "Enter" && node) {
      e.preventDefault();
      if (node.dir) {
        st.lib.expanded[node.rel] = st.lib.expanded[node.rel] === false;
        renderLibTree();
        saveSession();
      } else {
        openLibFile(node.rel);
      }
    }
  });
}

/* Ctrl+\ — bounce focus between the tree (left) and the editor (right) */
function libToggleFocus() {
  if (st.activeTab !== "library" || !st.lib.ui) return;
  const { panel, treeHost, editorHost } = st.lib.ui;
  const inLeft = panel.querySelector(".lib-left")?.contains(document.activeElement);
  if (inLeft) {
    if (st.lib.editor) st.lib.editor.focus();
    else editorHost?.focus();
  } else {
    if (!st.lib.cursor) {
      st.lib.cursor = st.lib.open || libVisibleRows()[0]?.rel || null;
    }
    treeHost.focus();
    renderLibTree();
  }
}

function rootCtxItems(baseRel) {
  const prefix = baseRel ? baseRel + "/" : "";
  return [
    {
      label: "New file…",
      fn: () => promptModal("New file", "Path inside the library:", prefix,
        (v) => v.trim() && libCreate(v.trim(), false), "Create"),
    },
    {
      label: "New folder…",
      fn: () => promptModal("New folder", "Path inside the library:", prefix,
        (v) => v.trim() && libCreate(v.trim(), true), "Create"),
    },
    "-",
    { label: "Collapse all", fn: libCollapseAll },
    {
      label: (st.lib.showHidden ? "✓ " : "") + "Show hidden files",
      fn: () => {
        st.lib.showHidden = !st.lib.showHidden;
        loadLibTree();
        saveSession();
      },
    },
  ];
}

function renderLibTree() {
  const host = st.lib.ui.treeHost;
  host.replaceChildren();
  const render = (nodes, depth) => {
    const frag = document.createDocumentFragment();
    for (const n of nodes) {
      const row = el("div", {
        class: "tree-row" + (n.dir ? " dir" : "")
          + (st.lib.open === n.rel ? " sel" : "")
          + (st.lib.cursor === n.rel ? " kbd-cursor" : "")
          + (n.hidden ? " hid" : ""),
        title: n.rel,
      });
      if (n.dir) {
        const open = st.lib.expanded[n.rel] !== false;   // default expanded
        row.append(
          el("span", { class: "tree-caret", text: open ? "▾" : "▸" }),
          el("span", { html: icon("folder", 13) }),
          el("span", { text: n.name }));
        row.addEventListener("click", () => {
          st.lib.cursor = n.rel;
          st.lib.expanded[n.rel] = !open;
          renderLibTree();
          saveSession();
        });
        row.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          e.stopPropagation();
          ctxMenu(e.clientX, e.clientY, [
            ...rootCtxItems(n.rel),
            "-",
            { label: "Rename…", fn: () => libRenamePrompt(n.rel) },
            { label: "Delete folder", danger: true, fn: () => libDeletePrompt(n, true) },
          ]);
        });
        frag.append(row);
        if (open && n.children?.length) {
          const kids = el("div", { class: "tree-kids" });
          kids.append(render(n.children, depth + 1));
          frag.append(kids);
        }
      } else {
        row.append(
          el("span", { class: "tree-caret" }),
          el("span", { html: icon(iconFor(n.name), 13) }),
          el("span", { text: n.name }));
        row.addEventListener("click", () => {
          st.lib.cursor = n.rel;
          openLibFile(n.rel);
        });
        row.addEventListener("contextmenu", (e) => {
          e.preventDefault();
          e.stopPropagation();
          ctxMenu(e.clientX, e.clientY, [
            { label: "Open", fn: () => openLibFile(n.rel) },
            "-",
            ...rootCtxItems(parentRel(n.rel)),
            { label: "Rename…", fn: () => libRenamePrompt(n.rel) },
            { label: "Delete file", danger: true, fn: () => libDeletePrompt(n, false) },
          ]);
        });
        frag.append(row);
      }
    }
    return frag;
  };
  host.append(render(st.lib.tree, 0));
  host.querySelector(".kbd-cursor")?.scrollIntoView({ block: "nearest" });
}

function parentRel(rel) {
  const i = rel.lastIndexOf("/");
  return i < 0 ? "" : rel.slice(0, i);
}

/* a markdown link target → library-relative path, resolved against the
 * open file's folder ("/x" is library-root absolute; ".." walks up) */
function resolveLibLink(target) {
  let t = String(target || "").split("#")[0].split("?")[0].trim();
  if (!t) return null;
  let parts;
  if (t.startsWith("/")) {
    parts = t.split("/");
  } else {
    const dir = st.lib.open ? st.lib.open.split("/").slice(0, -1) : [];
    parts = dir.concat(t.split("/"));
  }
  const out = [];
  for (const p of parts) {
    if (!p || p === ".") continue;
    if (p === "..") out.pop();
    else out.push(p);
  }
  return out.join("/") || null;
}

function renderLibResults(results, q) {
  const host = st.lib.ui.resHost;
  host.replaceChildren();
  if (!results.length) {
    host.append(el("div", { class: "picker-empty", text: "No matches." }));
    return;
  }
  for (const r of results) {
    if (r.kind === "file") {
      host.append(el("div", {
        class: "res-row res-file", title: r.rel, text: r.rel,
        onclick: () => openLibFile(r.rel),
      }));
    } else {
      const row = el("div", {
        class: "res-row res-line", title: `${r.rel}:${r.line}`,
        onclick: () => openLibFile(r.rel),
      });
      const idx = r.text.toLowerCase().indexOf(q.toLowerCase());
      row.append(el("span", { class: "res-loc", text: `${r.rel}:${r.line} ` }));
      if (idx >= 0) {
        row.append(
          document.createTextNode(r.text.slice(0, idx)),
          el("b", { text: r.text.slice(idx, idx + q.length) }),
          document.createTextNode(r.text.slice(idx + q.length)));
      } else {
        row.append(document.createTextNode(r.text));
      }
      host.append(row);
    }
  }
}

/* ---------- open / save / discard ---------- */

/* the single navigation gate: unsaved changes must be saved or discarded */
function libDirtyGate(then) {
  if (!st.lib.dirty) { then(); return; }
  modal("Unsaved changes",
    [el("p", { text: `"${st.lib.open}" has unsaved changes.` })],
    [
      { label: "Cancel" },
      { label: "Discard", cls: "btn-danger", fn: () => { st.lib.dirty = false; then(); } },
      {
        label: "Save & continue", cls: "btn-acc",
        fn: () => { saveLibFile().then((ok) => { if (ok) then(); }); },
      },
    ]);
}

async function openLibFile(rel) {
  libDirtyGate(async () => {
    const res = await Api.call("lib_read", rel);
    if (!res.ok) { toast(res.error, "err"); return; }
    if (res.data.binary) {
      toast(rel + " is a binary file — the editor only opens text.", "warn");
      return;
    }
    if (st.lib.editor) { st.lib.editor.destroy(); st.lib.editor = null; }
    st.lib.open = rel;
    st.lib.dirty = false;
    const host = st.lib.ui.editorHost;
    host.replaceChildren();
    const conf = editorModeFor(rel);
    st.lib.editor = new LoomEditor(host, {
      text: res.data.text,
      mode: conf.mode,
      lang: conf.lang,
      onDirty: (d) => { st.lib.dirty = d; renderLibFilebar(); renderTabs(); },
      onSave: () => saveLibFile(),
      // Ctrl+Click on a relative markdown link opens that document
      onOpenLink: (target) => {
        const dest = resolveLibLink(target);
        if (dest) openLibFile(dest);
      },
    });
    // editor scroll: track for the session; restore once when this open
    // came from a session replay
    st.lib.scrollPos = 0;
    const sc = st.lib.editor.scroller;
    sc.addEventListener("scroll", () => {
      if (!sc.clientHeight) return;   // hidden tab reads 0 — keep the real pos
      st.lib.scrollPos = sc.scrollTop;
      saveSession();
    }, { passive: true });
    if (st.lib.restoreScroll != null) {
      const v = st.lib.restoreScroll;
      st.lib.restoreScroll = null;
      requestAnimationFrame(() => {
        sc.scrollTop = v;
        st.lib.scrollPos = v;
      });
    }
    st.lib.editor.focus();
    renderLibFilebar();
    renderLibTree();
    renderTabs();
    saveSession();
    pushNav({ tab: "library", file: rel });
  });
}

async function saveLibFile() {
  if (!st.lib.open || !st.lib.editor) return false;
  const res = await Api.call("lib_write", st.lib.open, st.lib.editor.getValue());
  if (!res.ok) { toast(res.error, "err"); return false; }
  st.lib.editor.markClean();
  st.lib.dirty = false;
  renderLibFilebar();
  renderTabs();
  toast("Saved " + st.lib.open, "ok", 1800);
  return true;
}

async function discardLibFile() {
  if (!st.lib.open) return;
  const res = await Api.call("lib_read", st.lib.open);
  if (!res.ok) { toast(res.error, "err"); return; }
  st.lib.editor.setText(res.data.text);
  st.lib.dirty = false;
  renderLibFilebar();
  renderTabs();
}

function renderLibFilebar() {
  const bar = st.lib.ui?.filebar;
  if (!bar) return;
  bar.replaceChildren();
  if (!st.lib.open) {
    bar.append(el("span", { class: "lib-filename", text: "no file open" }));
    return;
  }
  bar.append(
    el("span", { html: icon(iconFor(st.lib.open), 13) }),
    el("span", { class: "lib-filename" + (st.lib.dirty ? " dirty" : ""), text: st.lib.open }),
    el("span", { class: "spacer" }));
  if (st.lib.dirty) {
    bar.append(
      el("button", { class: "btn btn-sm", text: "Discard", onclick: () => confirmModal("Discard changes", "Throw away the unsaved changes to " + st.lib.open + "?", "Discard", discardLibFile, true) }),
      el("button", { class: "btn btn-sm btn-acc", text: "Save  (Ctrl+S)", onclick: saveLibFile }));
  }
}

/* ---------- create / rename / delete ---------- */
async function libCreate(rel, directory) {
  const res = await Api.call("lib_create", rel, directory);
  if (!res.ok) { toast(res.error, "err"); return; }
  await loadLibTree();
  if (!directory) openLibFile(rel);
}

function libRenamePrompt(rel) {
  promptModal("Rename", "New path inside the library:", rel, async (v) => {
    const to = v.trim();
    if (!to || to === rel) return;
    const res = await Api.call("lib_rename", rel, to);
    if (!res.ok) { toast(res.error, "err"); return; }
    if (st.lib.open === rel) st.lib.open = to;
    await loadLibTree();
    renderLibFilebar();
  }, "Rename");
}

function libDeletePrompt(node, isDir) {
  confirmModal("Delete " + (isDir ? "folder" : "file"),
    (isDir ? "Delete the folder and EVERYTHING in it: " : "Delete: ")
      + node.rel + "?",
    "Delete", async () => {
      const res = await Api.call("lib_delete", node.rel);
      if (!res.ok) { toast(res.error, "err"); return; }
      if (st.lib.open && (st.lib.open === node.rel || st.lib.open.startsWith(node.rel + "/"))) {
        st.lib.open = null;
        st.lib.dirty = false;
        if (st.lib.editor) { st.lib.editor.destroy(); st.lib.editor = null; }
        st.lib.ui.editorHost.replaceChildren(
          el("div", { class: "lib-placeholder", text: "File deleted." }));
        renderLibFilebar();
      }
      loadLibTree();
    }, true);
}

/* a file was rewritten OUTSIDE the editor (the model wizard appends to
 * loom.yaml, the Environments tab writes environments.yaml): reload the
 * open buffer so it never shows — or worse, SAVES — a stale version.
 * Unsaved user edits are never clobbered; they get a warning instead. */
async function libReloadIfOpen(rel) {
  if (!st.lib.editor || st.lib.open !== rel) return;
  if (st.lib.dirty) {
    toast(rel + " changed on disk. Your unsaved edits are kept — Discard "
      + "to load the new version, or save to overwrite it.", "warn", 8000);
    return;
  }
  const res = await Api.call("lib_read", rel);
  if (!res.ok) return;
  const sc = st.lib.editor.scroller;
  const pos = sc.scrollTop;
  st.lib.editor.setText(res.data.text);
  st.lib.dirty = false;
  renderLibFilebar();
  requestAnimationFrame(() => { sc.scrollTop = pos; });
}

/* jump straight to a file (Servers tab → Edit loom.yaml) */
function openLibraryFileAt(rel) {
  openTab("library");
  // the tab may have just mounted; give the DOM a beat
  setTimeout(() => openLibFile(rel), 30);
}
