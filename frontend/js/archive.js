/* archive.js — the Chat Archive tab (singleton). Closed chats land here;
 * clicking one re-opens it as a chat tab (un-archives). */
"use strict";

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
  panel.append(el("div", { class: "srv-head" },
    el("h2", { text: "Chat Archive" }),
    el("div", { class: "arc-head-actions" },
      el("span", { class: "arc-meta", text: archived.length + " archived" }),
      setHotkey(el("button", {
        class: "btn btn-sm btn-acc", text: "New chat",
        title: "Start a fresh chat  (Ctrl+N)",
        onclick: () => newChat(),
      }), "Ctrl+N"))));
  const list = el("div", { class: "arc-list" });
  if (!archived.length) {
    list.append(el("div", { class: "picker-empty", text: "No archived chats. Closing a chat tab archives it here." }));
  }
  for (const c of archived) {
    list.append(el("div", {
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
      el("span", { class: "arc-meta", text: (c.model || "") }),
      el("span", { class: "arc-meta" },
        c.messages + " msgs · ", tago(c.updatedTs))));
  }
  panel.append(list);
}
