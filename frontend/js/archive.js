/* archive.js - the Chat Archive tab (singleton). Closed chats land here;
 * clicking one re-opens it as a chat tab (un-archives). The header's
 * search filters titles, models and providers. */
"use strict";

const ArcView = { q: "" };   // in-memory; resets per launch

function mountArchiveTab(panel) {
  panel.classList.add("arctab");
  refreshArchiveTab();
}

async function refreshArchiveTab() {
  const panel = panelFor("archive");
  if (!panel) return;
  const res = await Api.call("chats_list");
  if (!res.ok) return;
  const archived = res.data.chats.filter((c) => c.archived);

  panel.replaceChildren();
  const searchIn = el("input", {
    type: "text", class: "arc-search", value: ArcView.q,
    placeholder: "search the archive…",
  });
  searchIn.addEventListener("input", () => {
    ArcView.q = searchIn.value;
    paintList();
  });
  panel.append(el("div", { class: "srv-head" },
    el("h2", { text: "Chat Archive" }),
    el("div", { class: "arc-head-actions" },
      searchIn,
      el("span", { class: "arc-meta", text: archived.length + " archived" }),
      setHotkey(el("button", {
        class: "btn btn-sm btn-acc", text: "New chat",
        title: "Start a fresh chat  (Ctrl+N)",
        onclick: () => newChat(),
      }), "Ctrl+N"))));

  const list = el("div", { class: "arc-list" });
  panel.append(list);

  const row = (c) => el("div", {
    class: "arc-row",
    tabindex: "0",
    onclick: () => openChat(c.id),
    onkeydown: (e) => {
      if (e.key === "Enter") { e.preventDefault(); openChat(c.id); }
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
        { label: "Re-open", fn: () => openChat(c.id) },
        "-",
        {
          label: "Delete permanently", danger: true,
          fn: () => confirmModal("Delete chat",
            `Permanently delete "${c.title}"? This cannot be undone.`,
            "Delete", async () => {
              await Api.call("chat_delete", c.id);
              refreshArchiveTab();
            }, true),
        },
      ]);
    },
  },
    el("span", { html: icon("chat", 14) }),
    el("span", { class: "arc-title", text: c.title }),
    el("span", { class: "arc-meta",
      text: (c.provider ? c.provider + " · " : "") + (c.model || "") }),
    el("span", { class: "arc-meta" },
      c.messages + " msgs · ", tago(c.updatedTs)));

  const paintList = () => {
    const q = ArcView.q.trim().toLowerCase();
    const hits = archived.filter((c) => !q
      || [c.title, c.model, c.provider].some(
        (f) => String(f || "").toLowerCase().includes(q)));
    list.replaceChildren();
    if (!hits.length) {
      list.append(el("div", { class: "picker-empty",
        text: q ? "No archived chats match."
          : "No archived chats. Closing a chat tab archives it here." }));
    }
    for (const c of hits) list.append(row(c));
  };
  paintList();
}
