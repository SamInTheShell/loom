/* envstab.js - the Environments tab (singleton). An environment is a
 * named set of env vars a chat can load into its shell containers.
 *
 * Storage split (the whole point):
 *   - definitions - names, PLAIN values, and secret STUBS - live in the
 *     library's environments.yaml: shareable, and the stubs document
 *     which variables a container needs;
 *   - secret VALUES live in this machine's OS keyring only. They never
 *     enter the library and never even cross back into this UI - the
 *     editor only shows whether a value is set here.
 */
"use strict";

function envsState() {
  return st.envsTab || (st.envsTab = { names: [], open: null, rows: [] });
}

function mountEnvsTab(panel) {
  panel.classList.add("envstab");
  refreshEnvsTab();
}

async function refreshEnvsTab() {
  const panel = panelFor("envs");
  if (!panel) return;
  const es = envsState();
  const res = await Api.call("envs_list");
  es.names = res.ok ? res.data.envs || [] : [];
  if (es.open && !es.names.includes(es.open)) {
    es.open = null;
    es.rows = [];
  }
  renderEnvsTab();
}

async function openEnv(name) {
  const es = envsState();
  const res = await Api.call("env_get", name);
  if (!res.ok) { toast(res.error, "err"); return; }
  es.open = name;
  es.rows = (res.data.vars || []).map((r) => ({ ...r, _newSecret: "" }));
  renderEnvsTab();
}

function renderEnvsTab() {
  const panel = panelFor("envs");
  if (!panel) return;
  const es = envsState();
  panel.replaceChildren();
  // one centered column like the other utility tabs - without it the
  // hint paragraph spanned the whole window while the editor didn't
  const page = el("div", { class: "mods-wrap" });

  page.append(el("div", { class: "srv-head" },
    el("h2", { text: "Environments" }),
    el("button", {
      class: "btn btn-sm btn-acc", text: "New environment…",
      onclick: () => promptModal("New environment",
        "Name (e.g. aws-dev, prod-readonly):", "", async (v) => {
          const name = v.trim();
          if (!name) return;
          const r = await Api.call("env_save", name, [], {});
          if (!r.ok) { toast(r.error, "err"); return; }
          es.names = r.data.envs;
          openEnv(name);
          libReloadIfOpen("environments.yaml");
        }, "Create"),
    })));
  page.append(el("p", { class: "mods-hint",
    text: "Chats load an environment from the pill next to the "
      + "permission mode. Plain values live in the library's "
      + "environments.yaml; secret values live in THIS machine's system "
      + "keyring - the library only carries the stub, so anyone opening "
      + "it can see which variables a container needs." }));

  const wrap = el("div", { class: "envs-wrap" });
  const list = el("div", { class: "envs-list" });
  if (!es.names.length) {
    list.append(el("div", { class: "picker-empty",
      text: "No environments yet." }));
  }
  for (const name of es.names) {
    list.append(el("div", {
      class: "popup-item" + (es.open === name ? " sel" : ""),
      onclick: () => openEnv(name),
    },
      el("span", { html: icon("key", 13) }),
      el("span", { class: "pi-name", text: name })));
  }
  wrap.append(list);

  if (es.open) wrap.append(envEditor(es));
  page.append(wrap);
  panel.append(page);
}

function envEditor(es) {
  const box = el("div", { class: "env-editor" });
  box.append(el("h3", { text: es.open }));

  const rowsHost = el("div", { class: "env-rows" });
  const renderRows = () => {
    rowsHost.replaceChildren();
    es.rows.forEach((r) => {
      const keyIn = el("input", {
        type: "text", class: "env-key", value: r.key,
        placeholder: "VARIABLE_NAME", spellcheck: "false",
      });
      keyIn.addEventListener("input", () => { r.key = keyIn.value; });
      let valIn;
      if (r.secret) {
        valIn = el("input", {
          type: "password", class: "env-val", value: r._newSecret,
          placeholder: r.hasSecret
            ? "stored in the keyring - type to replace"
            : "not set on this machine - type to set",
        });
        valIn.addEventListener("input", () => { r._newSecret = valIn.value; });
      } else {
        valIn = el("input", {
          type: "text", class: "env-val", value: r.value || "",
          placeholder: "value (stored in the library)", spellcheck: "false",
        });
        valIn.addEventListener("input", () => { r.value = valIn.value; });
      }
      const secretChk = el("input", { type: "checkbox" });
      secretChk.checked = !!r.secret;
      secretChk.addEventListener("change", () => {
        r.secret = secretChk.checked;
        if (r.secret) r.value = "";
        renderRows();
      });
      rowsHost.append(el("div", { class: "env-row" },
        keyIn, valIn,
        el("label", { class: "chk", title: "Secret: the library keeps only "
          + "the stub; the value stays in this machine's keyring" },
          secretChk, "secret"),
        r.secret && r.hasSecret ? el("button", {
          class: "btn btn-sm", text: "clear",
          title: "Remove this secret's value from the keyring",
          onclick: () => { r._newSecret = ""; r._clear = true; renderRows(); },
        }) : null,
        el("button", {
          class: "btn btn-sm", text: "×", title: "Remove this variable",
          onclick: () => {
            es.rows = es.rows.filter((x) => x !== r);
            renderRows();
          },
        })));
    });
    rowsHost.append(el("button", {
      class: "btn btn-sm", text: "+ add variable",
      onclick: () => {
        es.rows.push({ key: "", value: "", secret: false,
                       hasSecret: false, _newSecret: "" });
        renderRows();
      },
    }));
  };
  renderRows();
  box.append(rowsHost);

  box.append(el("div", { class: "env-actions" },
    el("button", {
      class: "btn btn-sm btn-danger", text: "Delete environment",
      onclick: () => confirmModal("Delete environment",
        `Delete "${es.open}"? Its keyring secrets on this machine are `
        + "removed too.", "Delete", async () => {
          const r = await Api.call("env_delete", es.open);
          if (!r.ok) { toast(r.error, "err"); return; }
          es.open = null;
          es.rows = [];
          refreshEnvsTab();
          libReloadIfOpen("environments.yaml");
        }, true),
    }),
    el("span", { class: "spacer" }),
    el("button", {
      class: "btn btn-sm btn-acc", text: "Save",
      onclick: async () => {
        const vars = es.rows
          .filter((r) => r.key.trim())
          .map((r) => ({ key: r.key.trim(),
                         value: r.secret ? "" : (r.value || ""),
                         secret: !!r.secret }));
        const secretVals = {};
        for (const r of es.rows) {
          if (!r.secret || !r.key.trim()) continue;
          if (r._clear && !r._newSecret) secretVals[r.key.trim()] = "";
          else if (r._newSecret) secretVals[r.key.trim()] = r._newSecret;
        }
        const r = await Api.call("env_save", es.open, vars, secretVals);
        if (!r.ok) { toast(r.error, "err"); return; }
        toast("Saved " + es.open, "ok", 1800);
        openEnv(es.open);   // re-pull: hasSecret badges refresh
        libReloadIfOpen("environments.yaml");
      },
    })));
  return box;
}
