/* servers.js - the Providers tab (singleton, tab id "servers" for
 * session compatibility): every provider from loom.yaml as a card with
 * live reachability, its model list (pulled from the provider's own
 * API), a probe button, and a button that jumps to loom.yaml. Loom does
 * NOT manage inference processes - a provider is a llama-server
 * someone runs, or a hosted vendor API; all we do is talk to it. */
"use strict";

function mountServersTab(panel) {
  panel.classList.add("srvtab");
  panel.append(el("div", { class: "srv-cards", "data-role": "cards" }));
  refreshServersTab();
}

async function refreshServersTab() {
  const panel = panelFor("servers");
  if (!panel) return;
  const res = await Api.call("providers_get");
  if (!res.ok) { toast(res.error, "err"); return; }
  for (const p of res.data.providers || []) {
    st.providers[p.name] = { state: p.state, detail: p.detail,
                             models: p.models, ts: p.ts,
                             hasKey: !!p.hasKey };
  }
  st.config = res.data.config;
  renderServersTab();
}

/* set/clear one provider's API key (the upstream server's --api-key) -
 * stored in this machine's OS keyring, then re-probed */
function providerKeyDialog(name) {
  const live = st.providers[name] || {};
  const keyIn = el("input", { type: "password", autocomplete: "new-password",
    placeholder: live.hasKey ? "a key is set - type to replace it"
      : "the key this provider's --api-key expects" });
  const apply = async (value) => {
    const r = await Api.call("provider_key_set", name, value);
    if (!r.ok) { toast(r.error, "err"); return; }
    toast(r.data.hasKey ? "Key set - re-probing " + name
      : "Key cleared for " + name, "ok", 2200);
    setTimeout(refreshServersTab, 400);   // the probe lands async
  };
  modal("API key · " + name,
    [el("p", { class: "wiz-hint",
       text: "The provider's API key (a vendor account key, or a "
         + "llama-server started with --api-key). Sent in the vendor's "
         + "auth header on every request. "
         + "Stored in this machine's OS keyring, never in loom.yaml." }),
     keyIn],
    [
      { label: "Cancel" },
      live.hasKey ? { label: "Clear key", cls: "btn-danger",
        fn: () => { apply(""); } } : null,
      { label: "Set key", cls: "btn-acc",
        fn: () => {
          const v = keyIn.value.trim();
          if (!v) { toast("Type a key first (or Clear).", "warn"); return false; }
          apply(v);
        } },
    ].filter(Boolean), { id: "prov-key" });
  setTimeout(() => keyIn.focus(), 0);
}

function renderServersTab() {
  const panel = panelFor("servers");
  if (!panel) return;
  // never rebuild the panel out from under a live text selection
  const s = getSelection();
  if (s && s.rangeCount && !s.isCollapsed && panel.contains(s.anchorNode)) {
    clearTimeout(renderServersTab._retry);
    renderServersTab._retry = setTimeout(renderServersTab, 1200);
    return;
  }
  panel.replaceChildren();

  panel.append(el("div", { class: "srv-head" },
    el("h2", { text: "Providers" }),
    el("div", { style: "display:flex;gap:6px" },
      el("button", {
        class: "btn btn-sm btn-acc", text: "Add provider…",
        title: "Point Loom at a llama-server, or a hosted vendor (OpenAI, Anthropic, Gemini, Vertex AI, Bedrock)",
        onclick: () => providerDialog(),
      }),
      el("button", {
        class: "btn btn-sm", html: icon("refresh", 13) + " Probe all",
        title: "Re-check every provider and re-pull its model list",
        onclick: () => Api.call("providers_refresh"),
      }),
      el("button", {
        class: "btn btn-sm", html: icon("mcp", 13) + " MCP Servers",
        title: "Model Context Protocol tool servers for chats",
        onclick: () => openTab("mcpsrv"),
      }),
      el("button", {
        class: "btn btn-sm", html: icon("globe", 13) + " API Server",
        title: "Serve every provider's models as one OpenAI-compatible API",
        onclick: () => openTab("apisrv"),
      }),
      el("button", {
        class: "btn btn-sm", html: icon("gear", 13) + " Edit loom.yaml",
        title: "The Config tab - the whole file, validated on save",
        onclick: () => openTab("config"),
      }))));

  if (st.config?.error) {
    panel.append(el("div", { class: "srv-cfgerr", text: "loom.yaml: " + st.config.error }));
    return;
  }
  const provs = st.config?.providers || [];
  if (!provs.length) {
    panel.append(el("div", { class: "srv-empty" },
      el("p", { text: "No providers configured yet." }),
      el("p", { text: "Loom talks to a llama-server you run yourself "
        + "(directly or through an SSH tunnel, key auth only) - or to a "
        + "hosted vendor: OpenAI, Anthropic, Gemini, Vertex AI, Bedrock." }),
      el("div", { style: "display:flex;gap:8px;justify-content:center" },
        el("button", { class: "btn btn-acc", text: "Add provider…",
          onclick: () => providerDialog() }),
        el("button", {
          class: "btn", text: "Open loom.yaml",
          onclick: () => openTab("config"),
        }))));
    return;
  }
  const cards = el("div", { class: "srv-cards" });
  for (const p of provs) cards.append(providerCard(p));
  panel.append(cards);
}

function providerCard(p) {
  const live = st.providers[p.name] || {};
  const state = live.state || "unknown";
  const dot = state === "ok" ? "running" : state === "error" ? "error" : "stopped";

  const card = el("div", { class: "srv-card" });
  const probeBtn = el("button", {
    class: "btn btn-sm", html: icon("refresh", 12) + " Probe",
    title: "Check reachability and re-pull the model list now",
  });
  probeBtn.addEventListener("click", async () => {
    probeBtn.disabled = true;
    const r = await Api.call("provider_probe", p.name);
    probeBtn.disabled = false;
    if (!r.ok) { toast(r.error, "err"); return; }
    const got = r.data.provider;
    st.providers[p.name] = { state: got.state, detail: got.detail,
                             models: got.models, ts: got.ts };
    renderServersTab();
  });
  const row = el("div", { class: "srv-row" },
    el("span", { class: "dot " + dot }),
    el("span", { class: "srv-name", text: p.name }),
    el("span", { class: "srv-host",
      text: (PROVIDER_VENDORS.find((v) => v.id === p.vendor)?.label
        || p.vendor || "llama-cpp")
        + (p.ssh ? " · ssh " + p.ssh : "")
        + (live.hasKey ? " · key set" : "") }),
    el("span", { class: "srv-state" },
      el("span", { text: state === "ok" ? "reachable"
        : state === "error" ? "unreachable" : "not probed" })),
    el("div", { class: "srv-actions" },
      el("button", {
        class: "btn btn-sm", text: "Edit…",
        title: "Change this provider's entry in loom.yaml",
        onclick: () => providerDialog(p),
      }),
      el("button", {
        class: "btn btn-sm", html: icon("key", 12) + " Key",
        title: live.hasKey
          ? "An API key is stored for this provider - replace or clear it"
          : "Set the API key this provider's server expects (--api-key)",
        onclick: () => providerKeyDialog(p.name),
      }),
      probeBtn,
      el("button", {
        class: "btn btn-sm btn-danger", text: "Remove",
        title: "Remove this provider from loom.yaml",
        onclick: () => removeProviderPrompt(p),
      })));
  card.append(row);
  card.append(el("div", { class: "srv-meta", text: p.url }));
  if (state === "error") {
    card.append(el("div", { class: "srv-detail error", text: live.detail || "" }));
  }
  const models = live.models || [];
  if (models.length) {
    const list = el("div", { class: "srv-models" });
    for (const m of models) {
      list.append(el("div", { class: "srv-model-row" },
        el("span", { class: "srv-model-id", text: m.id }),
        el("span", { class: "srv-model-ctx",
          text: m.ctx ? fmtTok(m.ctx) + " ctx" : "" })));
    }
    card.append(list);
  } else if (state === "ok") {
    card.append(el("div", { class: "srv-detail", text: "no models listed" }));
  }
  if (live.ts) {
    card.append(el("div", { class: "srv-note" },
      "last probed ", (() => { const t = tago(live.ts); return t; })()));
  }
  return card;
}

/* the vendor catalog the dialog offers - mirrors providers.VENDORS on
 * the backend. Hosted vendors prefill their endpoint and REQUIRE a
 * key; llama-cpp is your own server (key optional, ssh available). */
const PROVIDER_VENDORS = [
  { id: "llama-cpp", label: "llama-cpp (llama-server)", url: "",
    urlPh: "http://127.0.0.1:8080",
    keyPh: "API key, if the server runs with --api-key (optional)" },
  { id: "openai", label: "OpenAI", url: "https://api.openai.com/v1",
    keyPh: "OpenAI API key (sk-…)" },
  { id: "anthropic", label: "Anthropic", url: "https://api.anthropic.com",
    keyPh: "Anthropic API key (sk-ant-…)" },
  { id: "gemini", label: "Google Gemini",
    url: "https://generativelanguage.googleapis.com/v1beta/openai",
    keyPh: "Google AI Studio API key" },
  { id: "vertex", label: "Google Vertex AI", url: "",
    urlPh: "https://<region>-aiplatform.googleapis.com/v1/projects/"
      + "<project>/locations/<region>/endpoints/openapi",
    keyPh: "OAuth access token (gcloud auth print-access-token - "
      + "expires ~hourly)" },
  { id: "bedrock", label: "Amazon Bedrock",
    url: "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
    keyPh: "Bedrock API key (bearer token)" },
];

/* ---------- the Add/Edit provider dialog ----------
 * name / vendor / url / optional ssh destination; Test probes without
 * writing anything. Add appends to loom.yaml; with `existing` (a config
 * entry) the dialog rewrites that entry in place - a rename moves the
 * keyring key along. Both writes are validated before landing. */
function providerDialog(existing) {
  const nameIn = el("input", { type: "text", placeholder: "workstation",
    value: existing?.name || "" });
  const typeSel = el("select", { class: "term-sel" },
    ...PROVIDER_VENDORS.map((v) =>
      el("option", { value: v.id, text: v.label })));
  typeSel.value = existing?.vendor || existing?.type || "llama-cpp";
  if (!typeSel.value) typeSel.value = "llama-cpp";
  const urlIn = el("input", { type: "text",
    placeholder: "http://127.0.0.1:8080", value: existing?.url || "" });
  const sshIn = el("input", { type: "text",
    placeholder: "user@host or a ~/.ssh/config alias (optional)",
    value: existing?.ssh || "" });
  const hasKey = !!(existing && st.providers[existing.name]?.hasKey);
  const keyIn = el("input", { type: "password", autocomplete: "new-password",
    placeholder: hasKey
      ? "a key is stored - leave empty to keep it, type to replace"
      : "API key, if the server runs with --api-key (optional)" });
  const status = el("div", { class: "srv-detail", text: "" });

  // per-vendor shaping: prefill the endpoint, adjust the key hint,
  // reserve ssh tunneling for your own servers - and be honest that
  // the hosted-vendor paths are NEW and lightly tested
  const sshRow = el("div", { class: "ts-row" },
    el("label", { text: "SSH" }), sshIn);
  const untestedNote = el("p", { class: "wiz-hint srv-untested",
    text: "⚠ Hosted-vendor support (OpenAI / Anthropic / Gemini / "
      + "Vertex AI / Bedrock) is new and UNTESTED against the live "
      + "APIs - if anything misbehaves, please report it so we can "
      + "improve it." });
  const vendMeta = () => PROVIDER_VENDORS.find(
    (v) => v.id === typeSel.value) || PROVIDER_VENDORS[0];
  let lastDefault = vendMeta().url;
  const applyVendor = () => {
    const v = vendMeta();
    // prefill the well-known endpoint, but never stomp a user-edited URL
    if (!urlIn.value.trim() || urlIn.value.trim() === lastDefault) {
      urlIn.value = v.url;
    }
    lastDefault = v.url;
    urlIn.placeholder = v.urlPh || v.url || "https://…";
    if (!hasKey) keyIn.placeholder = v.keyPh;
    sshRow.classList.toggle("hidden", v.id !== "llama-cpp");
    untestedNote.classList.toggle("hidden", v.id === "llama-cpp");
  };
  typeSel.addEventListener("change", applyVendor);

  const testBtn = el("button", { class: "btn btn-sm", text: "Test" });
  testBtn.addEventListener("click", async () => {
    status.textContent = "probing…";
    status.classList.remove("error");
    const r = await Api.call("provider_test", urlIn.value.trim(),
      sshIn.value.trim(), typeSel.value, keyIn.value.trim());
    if (!r.ok) { status.textContent = r.error; status.classList.add("error"); return; }
    const got = r.data.result;
    if (got.state === "ok") {
      status.textContent = "✓ reachable - "
        + (got.models || []).map((m) => m.id).join(", ");
    } else {
      status.textContent = got.detail || "unreachable";
      status.classList.add("error");
    }
  });

  applyVendor();
  modal(existing ? "Edit provider · " + existing.name : "Add provider",
    [el("p", { class: "wiz-hint",
       text: "A provider is an inference API: a llama-server you run "
         + "yourself, or a hosted vendor account. With an ssh "
         + "destination the URL is resolved FROM that host and all "
         + "traffic rides an ssh tunnel - SSH KEYS ONLY (a host that "
         + "asks for a password fails; load a key into ssh-agent). An "
         + "API key goes to this machine's OS keyring, never into "
         + "loom.yaml." }),
     el("div", { class: "ts-row" }, el("label", { text: "Name" }), nameIn),
     el("div", { class: "ts-row" }, el("label", { text: "Vendor" }), typeSel),
     el("div", { class: "ts-row" }, el("label", { text: "URL" }), urlIn),
     sshRow,
     el("div", { class: "ts-row" }, el("label", { text: "API key" }), keyIn),
     untestedNote,
     el("div", { class: "ts-row" }, el("label", { text: "" }), testBtn),
     status],
    [
      { label: "Cancel" },
      {
        label: (existing ? "Save" : "Add") + " to loom.yaml", cls: "btn-acc",
        fn: () => {
          const name = nameIn.value.trim();
          const url = urlIn.value.trim();
          if (!name || !url) {
            toast("A provider needs a name and a URL.", "warn");
            return false;   // keep the dialog open
          }
          const call = existing
            ? Api.call("provider_update", existing.name, name, typeSel.value,
                url, sshIn.value.trim(), keyIn.value.trim())
            : Api.call("provider_add", name, typeSel.value, url,
                sshIn.value.trim(), keyIn.value.trim());
          call.then((r) => {
            if (!r.ok) { toast(r.error, "err"); return; }
            toast((existing ? "Updated " : "Added ") + name + " in loom.yaml"
              + (keyIn.value.trim() ? " (key in the keyring)" : ""), "ok");
            refreshServersTab();
            libReloadIfOpen(st.lib.configFile || "loom.yaml");
          });
        },
      },
    ], { id: "add-provider" });
  setTimeout(() => nameIn.focus(), 0);
}

function removeProviderPrompt(p) {
  confirmModal("Remove provider",
    "Remove " + p.name + " from loom.yaml? Its API key (if any) is "
    + "deleted from the keyring. The inference server itself is not "
    + "Loom's - it keeps running.",
    "Remove", async () => {
      const r = await Api.call("provider_remove", p.name);
      if (!r.ok) { toast(r.error, "err"); return; }
      toast("Removed " + p.name + " from loom.yaml", "ok");
      refreshServersTab();
      libReloadIfOpen(st.lib.configFile || "loom.yaml");
    }, true, "prov-remove");
}
