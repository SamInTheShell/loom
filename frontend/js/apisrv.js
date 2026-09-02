/* apisrv.js — the API Server tab (singleton): an OpenAI-compatible API
 * over the managed llama-servers. Interface + port persist in loom.yaml
 * (`api:`); the on/off toggle is deliberately NOT persisted — every Loom
 * launch starts with the API off. The tray menu carries the same toggle.
 * Embeddings ride through /v1/embeddings (the target llama-server needs
 * --embeddings in its flags). */
"use strict";

function apiSrvState() {
  return st.apiSrv || (st.apiSrv = {
    running: false, interface: "", port: 0,
    cfg: null,             // {interface, port} from loom.yaml
    editIface: null, editPort: null,   // unsaved field edits
  });
}

function mountApiSrvTab(panel) {
  panel.classList.add("modstab");
  refreshApiSrvTab();
}

async function refreshApiSrvTab() {
  const as = apiSrvState();
  const r = await Api.call("api_server_status");
  if (r.ok) {
    as.running = !!r.data.api.running;
    as.interface = r.data.api.interface || "";
    as.port = r.data.api.port || 0;
    as.cfg = r.data.api.cfg || { interface: "127.0.0.1", port: 1234 };
  }
  renderApiSrvTab();
}

function renderApiSrvTab() {
  const panel = panelFor("apisrv");
  if (!panel) return;
  const as = apiSrvState();
  const focus = captureFocus(panel);
  panel.replaceChildren();
  const wrap = el("div", { class: "mods-wrap" });

  wrap.append(el("div", { class: "srv-head" }, el("h2", { text: "API Server" })));
  wrap.append(el("p", { class: "mods-hint",
    text: "Serve the running models as an OpenAI-compatible API "
      + "(/v1/chat/completions, /v1/completions, /v1/embeddings, "
      + "/v1/models). The toggle is never saved: every Loom launch starts "
      + "with the API OFF. It's also in the system tray menu." }));

  /* ---- the toggle + live status ---- */
  const base = "http://" + (as.running ? as.interface : (as.cfg?.interface || ""))
    + ":" + (as.running ? as.port : (as.cfg?.port || ""));
  const togBtn = el("button", {
    class: "btn " + (as.running ? "btn-danger" : "btn-acc"),
    text: as.running ? "Turn off" : "Turn on",
    onclick: async () => {
      const r = await Api.call("api_server_toggle", !as.running);
      if (!r.ok) { toast(r.error, "err"); return; }
      refreshApiSrvTab();
    },
  });
  wrap.append(el("div", { class: "api-status" + (as.running ? " on" : "") },
    el("span", { class: "dot " + (as.running ? "running" : "stopped") }),
    el("span", { class: "api-state",
      text: as.running ? "serving on " + base : "off" }),
    el("span", { class: "spacer" }),
    togBtn));

  /* ---- interface + port (persisted in loom.yaml `api:`) ---- */
  const ifaceIn = el("input", { type: "text", class: "mods-url",
    "data-keep": "api-iface", list: "api-iface-list",
    value: as.editIface ?? (as.cfg?.interface || "127.0.0.1") });
  const dl = el("datalist", { id: "api-iface-list" },
    el("option", { value: "127.0.0.1", label: "this machine only" }),
    el("option", { value: "0.0.0.0", label: "every interface (LAN!)" }));
  ifaceIn.addEventListener("input", () => { as.editIface = ifaceIn.value; });
  const portIn = el("input", { type: "number", class: "mods-url api-port",
    "data-keep": "api-port", min: "1", max: "65535",
    value: as.editPort ?? String(as.cfg?.port || 1234) });
  portIn.addEventListener("input", () => { as.editPort = portIn.value; });
  const save = el("button", {
    class: "btn btn-sm btn-acc", text: "Save to loom.yaml",
    title: "Persists interface + port (not the on/off state); a running "
      + "API restarts on the new address",
    onclick: async () => {
      const r = await Api.call("api_server_config_set",
        ifaceIn.value.trim() || "127.0.0.1", Number(portIn.value) || 1234);
      if (!r.ok) { toast(r.error, "err"); return; }
      as.editIface = as.editPort = null;
      toast("Saved to loom.yaml", "ok");
      refreshApiSrvTab();
      libReloadIfOpen(st.lib.configFile || "loom.yaml");
    },
  });
  wrap.append(el("div", { class: "mods-dl" },
    el("label", { class: "api-lbl", text: "interface" }), ifaceIn, dl,
    el("label", { class: "api-lbl", text: "port" }), portIn, save));
  if ((ifaceIn.value || "").trim() === "0.0.0.0") {
    wrap.append(el("p", { class: "mods-hint",
      text: "⚠ 0.0.0.0 exposes the API to your whole network — there is "
        + "no authentication." }));
  }

  /* ---- how to use it ---- */
  const usage =
    "curl " + base + "/v1/models\n\n"
    + "curl " + base + "/v1/chat/completions \\\n"
    + "  -H 'Content-Type: application/json' \\\n"
    + "  -d '{\"model\": \"<model name from loom.yaml>\",\n"
    + "       \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}'\n\n"
    + "curl " + base + "/v1/embeddings \\\n"
    + "  -H 'Content-Type: application/json' \\\n"
    + "  -d '{\"model\": \"<embedding model>\", \"input\": \"hello\"}'";
  const copyBtn = el("button", { class: "btn btn-sm",
    html: icon("copy", 12) + " Copy", title: "Copy the examples" });
  copyBtn.addEventListener("click", async () => {
    await copyText(usage);
    copyBtn.textContent = "✓ copied";
    setTimeout(() => { copyBtn.innerHTML = icon("copy", 12) + " Copy"; }, 1500);
  });
  const hint = el("div", { class: "mods-list" },
    el("div", { class: "mods-dir", style: "display:flex;align-items:center;gap:8px" },
      el("span", { text: "usage" }),
      el("span", { style: "flex:1" }), copyBtn),
    el("div", { class: "api-usage" },
      el("pre", { text: usage }),
      el("p", { class: "mods-hint",
        text: "Only RUNNING models serve (start them in Servers or a chat "
          + "— the API never starts servers itself). An unknown model name "
          + "routes to the single running model, so drop-in clients work. "
          + "Embeddings need a llama-server started with --embeddings in "
          + "its flags." })));
  wrap.append(hint);

  panel.append(wrap);
  restoreFocus(panel, focus);
}

/* pushed when the API flips on/off (tab, tray, or library close) */
function onApiSrvEvent(ev) {
  const as = apiSrvState();
  as.running = !!ev.running;
  as.interface = ev.interface || "";
  as.port = ev.port || 0;
  if (st.activeTab === "apisrv") renderApiSrvTab();
}
