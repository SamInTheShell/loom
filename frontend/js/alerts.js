/* alerts.js — the Alerts tab (singleton). Every toast is recorded as an
 * alert; the list PERSISTS per library across sessions (backed by
 * ~/.loom/state.json) until the user clears it. The bell in the top bar
 * opens this tab; its badge counts alerts not yet looked at. */
"use strict";

function mountAlertsTab(panel) {
  panel.classList.add("alrtab");
  refreshAlertsTab();
}

async function refreshAlertsTab() {
  const panel = panelFor("alerts");
  if (!panel) return;
  const res = await Api.call("alerts_get");
  const items = res.ok ? (res.data.alerts || []) : [];
  panel.replaceChildren();
  panel.append(el("div", { class: "srv-head" },
    el("h2", { text: "Alerts" }),
    el("div", { class: "alr-actions" },
      el("span", { class: "arc-meta", text: items.length + " kept" }),
      el("button", {
        class: "btn btn-sm", text: "Copy all",
        title: "Copy every alert to the clipboard",
        onclick: () => copyText(items.map((n) =>
          new Date(n.ts).toLocaleString() + "  [" + (n.level || "info")
          + "]  " + n.msg).join("\n")),
      }),
      el("button", {
        class: "btn btn-sm btn-danger", text: "Clear",
        title: "Delete every kept alert for this library",
        onclick: () => confirmModal("Clear alerts",
          "Delete every kept alert for this library? This cannot be undone.",
          "Clear", async () => {
            await Api.call("alerts_clear");
            refreshAlertsTab();
          }, true),
      }))));
  const list = el("div", { class: "notif-list alr-list" });
  if (!items.length) {
    list.append(el("div", { class: "picker-empty",
      text: "No alerts. Toasts and background events land here and stay — across sessions — until you clear them." }));
  }
  for (const n of [...items].reverse()) {
    const btn = el("button", { class: "btn btn-sm", text: "Copy" });
    btn.addEventListener("click", async () => {
      await copyText(n.msg);
      btn.textContent = "✓";
      setTimeout(() => { btn.textContent = "Copy"; }, 1200);
    });
    list.append(el("div", { class: "notif-row " + (n.level || "") },
      el("span", { class: "notif-when", text: new Date(n.ts).toLocaleString() }),
      el("div", { class: "notif-msg", text: n.msg }),
      btn));
  }
  panel.append(list);
}

/* the bell (and Ctrl+Shift+A): the tab is the alert surface now */
function openAlertsTab() {
  NotifLog.unseen = 0;
  renderNotifBadge();
  openTab("alerts");
}
