# JS app architecture — framework-free state, routing, rendering

The application architecture for a desktop-first HTML/CSS/JS app served
straight off disk (`file://`) inside a webview window, with a native
backend reachable only through an injected JS bridge (the Python side is
../python/pywebview/js-api-bridge.md). No framework, no bundler, no npm,
no dev server — plain script tags, one global state object, hash
routing, and full-view re-renders with a handful of surgical
escape hatches. This shape holds up well past 15k lines of UI code and
buys instant startup, zero build steps, and total debuggability; the
price is discipline, and this file is that discipline written down.
Widgets (element builder, menus, modals, tooltips) are components.md;
the visual system is design-language.md.

## Script order is the module system

Under a `file://` origin Chromium blocks ES-module imports and
`fetch()` of sibling files — but classic `<script src>` and
`<link rel=stylesheet>` work fine. So there are no modules: every file
is an ordinary script defining globals (each starts with
`"use strict";`), and the ONLY assembly step is rendering `index.html`
from a template at app launch (~1 ms, Jinja on the backend) with the
script tags in a fixed, meaningful order:

```
util.js        helpers: h(), toast, tooltip, formatting
data.js        seed dataset — the shapes ARE the API contract
state.js       the store: st, lookups, persistence, chrome renderers
api.js         bridge wrapper + mock backend + event registry
sim.js         the turn driver (streams, tools) mutating state
components.js  shared widgets     modals.js  dialogs
views_*.js     one file per page
router.js      routes + refreshAll
main.js        boot IIFE — wiring, event dispatch, start()
```

Later files may call earlier globals freely; an earlier file may
reference a later global only inside a function that runs after boot
(guard with `typeof fn === "function"` when unsure). Secondary windows
(editor, docs, diagnostics popouts) get their OWN composed page with a
subset of the list plus one page-local controller file — shared code is
shared by inclusion, not import. Static data a page needs is embedded
at compose time (`<script>window.DOCS = {{json}};</script>`), never
fetched.

## The state store

One mutable global object. No proxies, no observables, no reducers:

```js
const st = {
  route: { view: "list" },
  real: false,            // true when the native bridge is present
  items: SEED_ITEMS,      // server collections, refreshed via Api
  settings: SETTINGS,     // mirrored to backend config
  selectedId: null,       // ephemeral UI state
  panelFilter: "",        //   (session-scoped, never persisted)
  sel: new Set(),         //   multi-select
};
```

Views read `st` directly, mutate it directly, then trigger a repaint.
The store file also owns all lookups (`itemById` — linear `find` is
fine at these sizes), derived predicates, and the persistence helpers.
Three tiers of state, and the tier decides where a field lives:

- **Backend collections** (`st.items`, providers, …): loaded in bulk at
  boot, refreshed by re-fetching; expensive members load lazily (a
  record's transcript is `null` until first opened, then an
  `ensureLoaded(rec)` fetches and caches it on the record).
- **Persisted settings and session**: mirrored to the backend's config
  file. Durable preferences go on `cfg.*`; resumable UI state (open
  tabs, drafts, per-record selections, window history) goes under
  `cfg.session.*` — one namespace to inspect or wipe.
- **Ephemeral**: route, filters, selections, and `_`-prefixed caches
  (`st._fetchedAt`, `st._cfgCache`). Never written anywhere.

Persistence is read-modify-write through one helper, so writers never
clobber fields they don't own:

```js
async function persistSettings(patch) {
  if (!st.real) return;                  // mock mode: nothing to write
  try {
    const cfg = await Api.get("config_get");
    patch(cfg);                          // mutate the fresh copy
    await Api.call("config_set", cfg);
  } catch { /* Api.get already toasted */ }
}
// usage — the patch touches ONLY its own keys:
persistSettings((cfg) => {
  cfg.session = { ...(cfg.session || {}), openTabs: [...st.openTabs] };
});
```

Text-shaped state (drafts, filter boxes) debounces its writes — one
write per typing pause (~800 ms), not per keystroke — and every
persisted list normalizes on load through a function that migrates
legacy shapes losslessly, so old config files never break the UI.

## Rendering — full rebuild, with escape hatches

The base move is the cheapest one to reason about: wipe the view root
and rebuild it from state. Routing and re-rendering share one path:

```js
function render() {
  const hash = location.hash || "#/home";
  const root = document.getElementById("view");
  closeMenu(); Tooltip.hide();
  root.replaceChildren();
  for (const r of ROUTES) {
    const m = hash.match(r.re);
    if (m) { st.route.view = r.view; r.fn(m, root); syncChrome(); return; }
  }
  viewHome(root); syncChrome();          // fallback route
}
```

`refreshAll(lightweight)` is the single re-render entry point every
mutation calls. `lightweight = true` marks a stream tick (text grew,
nothing structural): the visible chat view gets an in-place patch, and
every other view refreshes only its chrome counters. `false` is a
structural change: full `render()`, but with focused-input rescue —
before rebuilding, the current view's text inputs save their value,
focus, and caret; after rebuilding, the new instances restore them. A
textarea that survives a rebuild with its caret intact is
indistinguishable from one that was never touched.

**Render parking.** A rebuild that lands between `mousedown` and
`mouseup` destroys the node under the cursor and the browser silently
drops the click; a rebuild while a context menu is open closes the
menu. During generation, refreshes arrive constantly — so any refresh
in one of those windows is PARKED and replayed just after:

```js
let held = false, parked = null;         // "light" | "full"
function refreshAll(light) {
  if (held || window._openMenu || window._animHold) {
    parked = (!light || parked === "full") ? "full" : "light";
    return;
  }
  /* …patch or render as above… */
}
document.addEventListener("pointerdown", () => { held = true; }, true);
for (const ev of ["pointerup", "pointercancel"])
  document.addEventListener(ev, () => {
    held = false;
    setTimeout(replayParked, 0);   // macrotask: the click fires FIRST,
  }, true);                        // on a still-live target
window.addEventListener("blur", () => {    // release outside the window
  held = false; setTimeout(replayParked, 0);
});
```

"Full wins over light" when both were parked. The `blur` clause
matters: a drag released outside the window never delivers `pointerup`,
and without it one stuck flag parks every future render forever. Menus
park rather than close because `render()` closes them; the parked
rebuild replays when the menu dismisses, and the menu items' closures
survive because they captured state, not nodes.

**In-place stream patching.** The high-frequency path never rebuilds.
The view snapshots what the patcher needs when it mounts, and the
patcher refuses anything structural:

```js
// view mount:
st._live = { id: rec.id, thread, count: rec.messages.length,
             status: rec.status, panelSig: panelSig() };
// stream tick:
function patchLiveView() {
  const v = st._live;
  if (!v || !v.thread.isConnected) return false;
  const rec = itemById(v.id);
  if (!rec || rec.messages.length !== v.count
           || rec.status !== v.status) return false;  // structure moved
  const last = rec.messages[rec.messages.length - 1];
  const body = [...v.thread.querySelectorAll(".msg .body")].pop();
  if (body) {
    body.innerHTML = renderMarkdown(last.text);       // grow in place
    if (rec._stick !== false) v.thread.scrollTop = v.thread.scrollHeight;
  }
  const sig = panelSig();                // sidebar: rebuild only when its
  if (sig !== v.panelSig) { renderPanel(); v.panelSig = sig; }
  else updateTimestampsInPlace();        // text nodes only
  return true;
}
```

Returning `false` hands control back to `refreshAll`, which does the
full render (with input rescue). Never try to patch structure — the
patch/rebuild boundary IS the contract. Signature strings (ids +
status + selection joined) are the change detector for list panels:
one string compare decides rebuild-vs-leave-alone, because rebuilding
rows on every streamed token kills the row under the cursor and eats
clicks. List ordering uses a key that moves only at DISCRETE moments
(user sent, turn ended) — never `updatedAt`, which ticks per token and
makes concurrently-streaming rows leapfrog on every delta.

**Live counters patch by data-attribute.** For high-frequency numeric
feeds (per-route load pushed several times a second), a full refresh
would rebuild settings inputs mid-typing. Instead the renderer stamps
`data-` keys on the value nodes, and the event handler patches ONLY
text content:

```js
function updateUsageDom() {
  document.querySelectorAll("[data-usage]").forEach((el) => {
    const u = st.usage[el.dataset.usage];
    if (u) el.textContent = `${u.inflight}/${u.capacity} in flight`;
  });
}
```

Render values into attributed nodes precisely so a later patcher can
find them — that is the whole pattern.

## The view-module contract

One `views_x.js` per page, defining a global `viewX(root, ...args)`.
The contract every view follows:

- Build the DOM with the element builder and append into `root`; bind
  events inline as `onclick`/`oninput` properties at build time. No
  event delegation framework — closures over state are the wiring.
- Call `setContext(crumbs, statusText)` so the shared chrome (bread-
  crumb, status bar) reflects the page.
- Read `st` directly. Any per-view state that must survive a re-render
  (active sub-tab, selected branch) lives ON `st`, not in the view's
  closure — the view is re-entered from scratch on every render and
  must reproduce itself from state alone.
- Mutating actions call `Api`, mutate `st` (or re-fetch), then
  `refreshAll()` or `navigate(...)`. Views never write config directly;
  they call the store's persistence helpers.
- Sub-sections that deserve a link are routes; transient ones are plain
  functions `tabX(body, model)` switched on an `st` field.
- Static chrome (filter inputs, buttons above a list) is wired ONCE at
  boot in main.js; the list renderer repaints only rows. Typing in a
  filter box must never lose focus to a repaint.

## Routing without URLs

A `file://` page has no server and no paths — `location.hash` is the
entire address bar. Routes are regex → function pairs:

```js
const ROUTES = [
  { re: /^#\/items$/,           fn: (m, root) => viewItems(root),      view: "items" },
  { re: /^#\/item\/([^/]+)$/,   fn: (m, root) => viewItem(root, m[1]), view: "item"  },
  { re: /^#\/old-name$/,        fn: () => redirect("#/items"),         view: "items" },
];
function navigate(hash) {
  if (location.hash === hash) render();   // hashchange won't fire — render
  else location.hash = hash;              // hashchange listener renders
}
function redirect(hash) {
  history.replaceState(null, "", hash);   // fires NO hashchange…
  render();                               // …so render explicitly
}
window.addEventListener("hashchange", render);
```

Rules that make this feel like a real router:

- **Redirecting routes must REPLACE**, never push: a pushed redirect
  hash stays in history, and Back lands on it and bounces forward again
  — Back appears broken. Keep legacy route aliases as redirects forever;
  persisted histories and muscle memory keep working.
- Back/forward come free from the browser; wire Alt+←/→ to
  `history.back()/forward()`.
- **Location restore**: persist the last ~50 hashes (debounced) under
  `cfg.session`; on boot, replay them with `history.pushState` — which
  fires no `hashchange` — so the back/forward stack is rebuilt silently
  and one `render()` lands on the final entry. An explicit hash in the
  opening URL always wins over the restored trail.
- Back/forward can land on data that went stale while the user was
  elsewhere: after rendering, quietly re-fetch if the last load is
  older than ~10 s.

## The bridge wrapper — two verbs, one error surface

Every backend method resolves to an `{ok, ...}` envelope; the wrapper
exposes exactly two verbs plus a mode probe:

```js
const Api = (() => {
  const real = () => !!(window.pywebview && window.pywebview.api);
  async function call(method, ...args) {         // never throws for
    if (real()) {                                // backend errors
      const res = await window.pywebview.api[method](...args);
      return res ?? { ok: false, error: "empty response" };
    }
    try { return await Mock[method](...args); }  // browser fallback
    catch (e) { return { ok: false, error: String(e?.message || e) }; }
  }
  async function get(method, ...args) {          // unwrap-or-surface
    const res = await call(method, ...args);
    if (!res.ok) {
      toast(res.error || method + " failed", "err");
      throw new Error(res.error || method + " failed");
    }
    return "data" in res ? res.data : res;
  }
  return { real, call, get };
})();
```

The convention: `get()` when the caller wants data — the error toasts
in exactly one place and the throw aborts the caller's chain; `call()`
when the caller inspects `.ok` itself or fires-and-forgets. Downstream
`catch {}` after `get()` is legitimate silence because the user was
already told. The `Mock` object mirrors the real backend method-for-
method against the seed dataset — the mock IS the API contract, keeps
the whole UI explorable in a plain browser, and forces the shapes to be
designed before the backend exists. `st.real` records which world the
page is in; views branch on it for honesty (an unmissable "DEMO DATA"
badge, features that answer "needs the desktop app").

Boot handles the bridge race: if the bridge is already injected, start;
otherwise listen for the ready event AND set a ~400 ms timeout that
starts in mock mode — a plain browser never fires the event. Load
depot + config in parallel, THEN install stateful chrome widgets, then
`render()`, then set a `window.READY = true` flag for automation.

## Backend events into the UI

The backend pushes events by evaluating one global function with a
single `{type, ...}` object (the transport is
../python/agent-harness/ui-bridge.md). A tiny registry fans out:

```js
const handlers = [];
function ON_EVENT(ev) {                    // called from the backend
  for (const fn of handlers) {
    try { fn(ev); } catch (e) { console.error("event handler", e); }
  }
}
function onEvent(fn) {
  handlers.push(fn);
  return () => {                           // unsubscribe — modals attach
    const i = handlers.indexOf(fn);        // per-run listeners
    if (i >= 0) handlers.splice(i, 1);
  };
}
```

main.js installs the main dispatcher: one `onEvent` with a `type`
switch. The conventions inside it carry the weight:

- **Coalesce bursts at the handler**: a commit event storms during
  agent work; a single debounce timer (~1.5 s) collapses the burst into
  one re-fetch + `refreshAll`.
- **Patch, don't render, for high-frequency types**: the usage feed
  updates `st` then calls the data-attribute patcher — never a render.
- **Refresh conditionally**: an index-progress event re-renders only if
  the settings page that shows it is the current view; otherwise it
  just updates state and toasts.
- **Scoped windows filter by identity**: a popout's handler ignores
  every event whose ids don't match its own binding.
- Toast dedupe for repeating errors: remember the last toasted detail,
  toast each distinct one once.

## Multi-window popouts

Secondary OS windows are separate webview pages, each composed with its
own script list. Three patterns, by how much state they need:

**Bound editor window** — shares the store/bridge/component scripts but
has NO router; it defines local shims (`function refreshAll() {
repaint(); }`, `function navigate() {}`) so shared components keep
working. Its identity is immutable for the window's lifetime, carried
in the location hash (`#repo=…&branch=…&mode=…`) and parsed once at
boot. It talks BACK through ordinary bridge calls: reporting its dirty
count (drives the app's close/quit warning gates), its mode, its open-
file context. It receives events filtered to its binding — a commit on
its branch triggers a debounced buffer refresh that fast-forwards clean
buffers (preserving the cursor) and flags dirty ones as conflicts
instead of overwriting; a `confirm_close` event renders the save/
discard dialog. Cross-window intent is a backend hop: "show this in the
main window" is a bridge call the backend routes, never a navigation.
The backend enforces one window per identity key.

**Self-contained document window** — takes only the util script and its
own controller; its data is embedded at compose time. The backend
focuses an already-open instance by calling a global the page exposes
(`window.OPEN_DOC = (slug, q) => { … }`) instead of spawning a
duplicate.

**Observer window (diagnostics)** — owns NO truth at all. The main
window holds live state (including in-flight streams), so it is the
only honest source: it tracks which observer windows exist via
open/close events (a `Set` of ids), and pushes compact snapshots on a
~700 ms interval — but only when the snapshot changed:

```js
const last = {};
function sendFeed(id, force) {
  const payload = buildSnapshot(itemById(id));
  const s = JSON.stringify(payload);
  if (!force && last[id] === s) return;    // dedupe by serialization
  last[id] = s;
  Api.call("observer_feed", id, payload);
}
setInterval(() => { for (const id of tracked) sendFeed(id); }, 700);
```

The live feed never carries large bodies; when the observer's user
clicks a row, it sends a detail-request event, the main window answers
with a one-shot detail payload, and the observer matches it to the
pending request by index. "Jump to this in the main app" is again an
event the main window handles by navigating itself. First paint sends
immediately on the open event, not on the next tick.

## Rules

- Load order is the dependency graph. A file's top level may only touch
  earlier globals; later globals only inside functions that run after
  boot. Adding a file means choosing its slot in the ordered list.
- Install stateful widgets AFTER persisted config loads. A pane-size
  handle built at boot captures the default sizes object, and persisted
  sizes silently never apply — a real, hard-to-spot bug class.
- `replaceState`/`pushState` fire no `hashchange` — call `render()`
  yourself (redirect) or deliberately don't (history restore).
  `navigate()` to the CURRENT hash must render explicitly too.
- Redirecting routes replace history; a pushed redirect breaks Back.
- Park renders while the pointer is down, a menu is open, or a layout
  animation runs; replay via `setTimeout(0)` after release so the click
  dispatches on a live target. Clear the pointer-held flag on window
  `blur` or one outside-release parks everything forever.
- Never rebuild a subtree containing a focused input: patch around it,
  or save value+focus+caret and restore onto the new instance. Guard
  hard against rebuilding under an in-place editor (bail out of the
  list repaint while the inline input exists).
- In-place patchers return `false` on any structural drift and the
  caller full-renders. Patch text and counters; never patch structure.
- Rebuild list panels only when a signature string changed; sort lists
  by discrete-moment keys, never by stream-ticking timestamps.
- Errors surface once: `get()` toasts and throws; a `call()` site owns
  its `.ok` check or explicitly accepts silence.
- Wrap every event subscriber in try/catch — one broken handler must
  not sever the feed for the rest.
- Keep the mock backend in method parity with the real one; it is the
  living API contract and the browser-preview mode.
- Popout windows never navigate and never own shared truth: bound
  windows report state through the bridge, observer windows render
  pushed snapshots, and cross-window actions round-trip through the
  backend.
- Dedupe pushed feeds by comparing serialized payloads; debounce all
  persistence (~300–800 ms) and event-driven refetches (~1–2 s).
- `requestAnimationFrame` never fires without a compositor (hidden or
  occluded webview) — use `setTimeout` for work that must run, rAF only
  for visible animation (node-graph.md uses it correctly).
