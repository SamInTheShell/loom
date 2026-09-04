/* apisrv.js - the API Server tab (singleton): one OpenAI-compatible API
 * aggregating every configured provider's models. Interface + port
 * persist in loom.yaml (`api:`); the on/off toggle is deliberately NOT
 * persisted - every Loom launch starts with the API off. The tray menu
 * carries the same toggle. */
"use strict";

function apiSrvState() {
  return st.apiSrv || (st.apiSrv = {
    running: false, interface: "", port: 0,
    cfg: null,             // {interface, port} from loom.yaml
    hasKey: false,         // an API key is stored in the OS keyring
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
    as.hasKey = !!r.data.api.hasKey;
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
    text: "Serve every provider's models as one OpenAI-compatible API "
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
      text: (as.running ? "serving on " + base : "off")
        + (as.hasKey ? " · key required" : "") }),
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
  if ((ifaceIn.value || "").trim() === "0.0.0.0" && !as.hasKey) {
    wrap.append(el("p", { class: "mods-hint",
      text: "⚠ 0.0.0.0 exposes the API to your whole network and no "
        + "key is set - anyone on it can use your providers." }));
  }

  /* ---- the API key (stored in THIS machine's OS keyring) ---- */
  const keyIn = el("input", { type: "password", class: "mods-url",
    "data-keep": "api-key", autocomplete: "new-password",
    placeholder: as.hasKey
      ? "a key is set - type to replace it"
      : "no key - anyone who can reach the port can use the API" });
  const setKey = async (value) => {
    const r = await Api.call("api_server_key_set", value);
    if (!r.ok) { toast(r.error, "err"); return; }
    as.hasKey = !!r.data.api.hasKey;
    toast(as.hasKey ? "API key set" + (as.running ? " - restarted" : "")
                    : "API key cleared", "ok", 2500);
    renderApiSrvTab();
  };
  const setBtn = el("button", { class: "btn btn-sm btn-acc", text: "Set key",
    title: "Store the key in this machine's OS keyring (never in "
      + "loom.yaml); a running API restarts so it applies now",
    onclick: () => {
      const v = keyIn.value.trim();
      if (!v) { toast("Type a key first (or use Clear).", "warn"); return; }
      setKey(v);
    } });
  const clearBtn = as.hasKey ? el("button", { class: "btn btn-sm",
    text: "Clear key", title: "Remove the key - the API becomes open again",
    onclick: () => confirmModal("Clear the API key?",
      "Requests will no longer need authentication.", "Clear",
      () => setKey(""), true, "api-key-clear") }) : null;
  wrap.append(el("div", { class: "mods-dl" },
    el("label", { class: "api-lbl", text: "api key" }), keyIn,
    setBtn, clearBtn));
  wrap.append(el("p", { class: "mods-hint",
    text: "With a key set, every route except /health requires "
      + "'Authorization: Bearer <key>' or 'x-api-key: <key>' - the same "
      + "contract llama-server and ninfer use. The key lives in this "
      + "machine's keyring, per library." }));

  /* ---- how to use it ---- */
  const auth = as.hasKey ? "  -H 'Authorization: Bearer <your key>' \\\n" : "";
  const usage =
    "curl " + (as.hasKey ? "-H 'Authorization: Bearer <your key>' " : "")
    + base + "/v1/models\n\n"
    + "curl " + base + "/v1/chat/completions \\\n" + auth
    + "  -H 'Content-Type: application/json' \\\n"
    + "  -d '{\"model\": \"<model id - or provider/model>\",\n"
    + "       \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}'\n\n"
    + "curl " + base + "/v1/embeddings \\\n" + auth
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
        text: "Requests route to the provider serving the model id "
          + "(disambiguate with provider/model). An unknown model name "
          + "routes to the single known model, so drop-in clients work. "
          + "Model lists come from the last probe - visit the Providers "
          + "tab if /v1/models comes back empty." })));
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
  if (ev.hasKey !== undefined) as.hasKey = !!ev.hasKey;
  if (st.activeTab === "apisrv") renderApiSrvTab();
}
