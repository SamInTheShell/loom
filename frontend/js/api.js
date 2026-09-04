/* api.js - bridge to the Python backend.
 *
 * Every call resolves to {ok, ...}. In a plain browser (no pywebview)
 * calls short-circuit to an error so the UI stays inspectable for CSS
 * work without pretending to have a backend.
 *
 * Events: Python pushes LM_onEvent({type:...}) via evaluate_js; handlers
 * registered here fan them out.
 */
"use strict";

const Api = (() => {
  function real() {
    return !!(window.pywebview && window.pywebview.api);
  }

  async function call(method, ...args) {
    if (!real()) return { ok: false, error: "no backend (browser preview)" };
    try {
      const res = await window.pywebview.api[method](...args);
      return res ?? { ok: false, error: "empty response from backend" };
    } catch (e) {
      return { ok: false, error: String(e?.message || e) };
    }
  }

  /* unwrap helper: returns .data (or full res) on ok, throws + toasts on error */
  async function get(method, ...args) {
    const res = await call(method, ...args);
    if (!res.ok) {
      toast(res.error || method + " failed", "err");
      throw new Error(res.error || method + " failed");
    }
    return "data" in res ? res.data : res;
  }

  return { real, call, get };
})();

/* ---------- event stream from Python ---------- */
const LM_events = { handlers: [] };
function LM_onEvent(ev) {
  for (const fn of LM_events.handlers) {
    try { fn(ev); } catch (e) { console.error("event handler", e); }
  }
}
function onLMEvent(fn) {
  LM_events.handlers.push(fn);
  return () => {
    const i = LM_events.handlers.indexOf(fn);
    if (i >= 0) LM_events.handlers.splice(i, 1);
  };
}
