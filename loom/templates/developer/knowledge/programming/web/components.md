# UI component library - framework-free widgets in plain JS

A desktop app's complete widget set - tooltips, menus, modals, toasts,
pickers, splitters, trees, collapsible cards - built with zero
dependencies: one tiny element helper, CSS classes for every visual
state, and a handful of hard-won positioning/dismissal/keyboard
conventions applied uniformly. Choose this over a component framework
when the app is a single long-lived page that re-renders by rebuilding
DOM subtrees (js-app-architecture.md): widgets are then just functions
returning elements, state lives in closures or on the element itself,
and there is nothing to mount, diff, or unmount. Tokens, colors and
the class naming aesthetic live in design-language.md; this file is
the mechanism catalog.

## The element helper

Everything is built through one function - a hyperscript-style `h()`
taking an emmet-ish spec, an optional attrs object, and children:

```js
function h(spec, attrs, ...kids) {
  const [tag, ...rest] = spec.split(/(?=[.#])/);
  const el = document.createElement(tag || "div");
  for (const p of rest) {
    const n = p.slice(1);
    if (!n) continue;                    // tolerate "div.a." from
    if (p[0] === ".") el.classList.add(n); // conditional class strings
    else el.id = n;
  }
  if (attrs && typeof attrs === "object"
      && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else if (k === "dataset")
        for (const [dk, dv] of Object.entries(v))
          if (dv != null) el.dataset[dk] = dv;
      else el.setAttribute(k, v === true ? "" : v);
    }
  } else if (attrs != null) kids.unshift(attrs);   // attrs was a child
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;        // conditionals: x && el
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
```

The load-bearing details: conditional class suffixes concatenate
(`"div.card" + (open ? ".open" : "")`), the attrs slot is optional
(a Node/string/array second argument shifts into children), `null`
and `false` children vanish so `cond && h(...)` composes, arrays
flatten to any depth, and strings become text nodes - `html:` is the
only innerHTML door and is used solely for the vendored inline-SVG
icon strings and sanitized markdown. Pair it with a `setChildren(el,
...kids)` that filters null/false before `replaceChildren` - the
native API stringifies null into the literal text "null".

Icons are a dictionary of inline `<svg viewBox="0 0 16 16">` strings;
`ico(name)` wraps one in `<span class="ico">` (14×14, `flex:none`,
`vertical-align:-2px`). No icon fonts, no external files.

## Conventions every widget shares

- **Tooltips via data-attribute**: any element sets
  `el.dataset.tip = "text"` (plus optional `dataset.tipKbd`) and the
  global tooltip engine does the rest. Never `title=` - it can't be
  styled and the OS places it ignorant of the window chrome.
- **State is a class**: `.pending`, `.denied`, `.sel`, `.open`,
  `.dragging`, `.collapsed`, `.menu-open` - JS toggles classes, CSS
  owns every visual. Chips/dots recolor purely by modifier class.
- **Non-selectable chrome**: `body { user-select: none; cursor:
  default }`; content opts back in with `.selectable, .selectable *
  { user-select: text; cursor: auto }`.
- **Viewport clamping**: every fixed-position overlay (tooltip, menu,
  popover, toast column) clamps into the viewport with a 6px margin.
  Nothing may ever render clipped off-screen.
- **z-index bands**: menus 150, toasts 200, search menus 250 (above
  modals at 100 - pickers must work inside dialogs), tooltip and
  pinned popovers 300, full-screen overlays 400.
- **Hotkey registry**: one `HOTKEYS = { id: {combo, label} }` table;
  the dispatcher fires a hotkey by CLICKING the element carrying
  `data-hk="<id>"`, so a key can never behave differently from the
  mouse, and `hkTip(text, id)` appends "- hotkey: Ctrl+X" to
  tooltips from the same table.

## Tooltip engine

One shared node (`div#tooltip`, `position:fixed; pointer-events:
none; visibility:hidden`) appended to `document.body`, driven by
delegated `mouseover`/`mouseout` on the document with
`closest('[data-tip]')` - per-element listeners would leak across
rebuilds. Show is delayed 350 ms; any `mousedown` (capture), window
`blur`, `resize`, or `scroll` (capture) hides instantly.

The placement math - measure real size first, prefer below, flip
above, clamp both axes:

```js
const M = 6;                              // viewport margin
function placeTip(n, target) {
  const r = target.getBoundingClientRect();
  const vw = innerWidth, vh = innerHeight;
  n.style.maxWidth = Math.min(320, vw - M * 2) + "px"; // 420 for cards
  n.style.left = "0px"; n.style.top = "-9999px";       // park offscreen
  n.style.visibility = "hidden";
  void n.offsetWidth;                                  // force layout
  const tw = n.offsetWidth, th = n.offsetHeight;       // now measurable
  let top = r.bottom + M;                              // prefer below
  if (top + th > vh - M) top = r.top - th - M;         // flip above
  if (top < M)                                         // neither fits:
    top = Math.max(M, Math.min(vh - th - M, r.bottom + M));
  let left = r.left + r.width / 2 - tw / 2;            // center on target
  left = Math.max(M, Math.min(left, vw - tw - M));     // clamp X
  n.style.left = Math.round(left) + "px";
  n.style.top = Math.round(top) + "px";
  n.style.visibility = "visible";
}
```

Content: `esc(dataset.tip)` plus a dim `<span class="tip-kbd">` for
the hotkey hint. A RICH variant - an element property `el._tipCard =
htmlOrFn` (a property, not dataset: it carries HTML) - switches the
node to `.card` styling (wider, grid rows) but stays hover-only and
pointer-inert. Widgets that move or rebuild under the cursor call
`Tooltip.hide()` themselves (splitter drags, meter clicks, renders).

## Toasts

Transient notifications stack in a fixed column bottom-right
(`#toast-root`, above the status bar). `toast(msg, kind)` builds
`div.toast(.ok|.warn|.err)` - kind only recolors the 3px left border
- with a message span and an X button. Auto-dismiss after 3.2 s via a
0.25 s opacity fade; the X clears both timers and removes at once.
Entry is a 0.15 s rise animation. The message span is `.selectable`
so users can copy error text. Return the element so callers can hold
long-lived toasts.

## Menus - anchored dropdowns and context menus

`openMenu(anchor, items)` renders a fixed `div.menu` of rows
(`{icon, label, onclick, danger?, tip?}`, with the string `"-"` as a
separator) and positions it against the anchor. The SAME function
serves context menus: a `{x, y}` anchor is adapted into a fake rect -

```js
if (anchor && !(anchor instanceof Element)) {
  const { x, y } = anchor;
  anchor = { getBoundingClientRect:
    () => ({ left: x, top: y, bottom: y, right: x, width: 0, height: 0 }) };
}
```

- so `contextmenu` handlers just call `openMenu({x: e.clientX, y:
e.clientY}, …)` after `preventDefault()`. Positioning: append to
body, read `offsetWidth/Height`, then

```js
let left = Math.min(r.left, innerWidth - mw - 6);
let top  = r.bottom + 4;
if (top + mh > innerHeight - 6) top = r.top - mh - 4;  // flip above
menu.style.left = Math.max(6, left) + "px";
menu.style.top  = Math.max(6, top) + "px";
```

with `max-height: calc(100vh - 24px); overflow-y: auto` so a long
menu scrolls instead of escaping the window.

Dismissal and keyboard rules (identical for every menu kind):

- ONE menu at a time: `window._openMenu` holds it; opening anything
  calls `closeMenu()` first.
- Outside dismissal is a document-capture `mousedown` handler that
  closes when the target is outside the menu - attached inside
  `setTimeout(…, 0)` so the click that OPENED the menu cannot
  self-dismiss it.
- Keyboard (`keydown`, document, capture): arrows move a highlight,
  Home/End jump, Enter activates, Escape/Tab close. Listening on the
  document, not the menu, matters: the app's render loop may steal
  focus at any moment and a focused-element handler dies with it.
  The moved highlight calls `scrollIntoView({block:"nearest"})`.
- The anchor button gets class `.menu-open` (accent-filled) while its
  menu is open, cleared in `closeMenu()` - the user must always see
  which control owns the open menu.
- `closeMenu()` removes the element, the key handler, the closer, and
  the anchor mark, then replays any re-render that was parked while
  the menu was open (js-app-architecture.md's render gate).

Hover highlight NEVER repaints rows: a repaint on `mouseenter`
rebuilds the node under the cursor and eats the click gesture (real
mice fire mouseenter before mousedown). Hover is CSS `:hover`;
mouseenter only syncs the keyboard index.

## Search menu - filterable, hierarchical picker

The workhorse dropdown for programmatic lists (providers, models,
branches, containers, folders): a `div.menu.searchmenu` with a filter
input in a head row, a scrolling `.sm-list` (max-height 300px), and
optionally a refresh icon-button. Options
(`opts.items`) are an array OR a sync/async function returning
`{icon, label, detail?, tip?, selected?, danger?, keepOpen?,
onclick, children?, action?}`.

- **Submenus**: `children` (array or thunk) turns a row into a
  drill-down level; entering pushes `{title, items}` onto a stack and
  prepends a "‹ back" row. ArrowRight enters, ArrowLeft and Escape
  pop one level (only when the query is empty - otherwise they are
  caret movement / close).
- **Search flattens the tree**: any non-empty query switches to a
  flattened pool where nested items carry their path as breadcrumb
  detail (`"Provider › label"`), so typing finds a model without
  drilling into its provider.
- **Free-form entry**: with `opts.custom`, typed text matching no
  item appends a `Use "…"` row calling `custom.onPick(text)` - for
  lists that are never exhaustive (model ids).
- **keepOpen rows** run `onclick` then reload the list in place
  (toggles that should show their new ✓ without reopening).
- **Row action button**: an optional trailing icon button (e.g. a
  gear jumping to the setting that manages the row) whose click must
  `stopPropagation()` so it never activates the row itself.
- **Long details** (> 36 chars) move onto their own line under the
  label - inline they crush the label to one letter. `wrapLabels`
  makes labels wrap instead of ellipsizing (model names differ in
  their tails; never "…" them).
- **Selection marks**: `selected: true` renders a trailing accent ✓.
- **Typing pulls focus**: the document-level key handler forwards
  printable keys into the filter input if focus wandered, so search
  works the instant the menu opens regardless of focus.
- **Refresh**: `onRefresh` re-fetches the source ("Refreshing…"
  placeholder row), then `items()` re-runs; open drill-down levels
  re-resolve against the fresh tree by label path so ✓ marks stay
  honest inside submenus.
- **Placement re-runs after EVERY paint** using the menu's real size
  (same clamp/flip as openMenu, plus `align:"right"` to flush right
  edges and a `width` override capped at `calc(100vw - 12px)`).
  Guessing final height for the flip-above case floats small menus
  in mid-air; measuring after each filter keystroke keeps the menu
  attached to its anchor as it grows and shrinks.

Picker buttons are all the same composition: a `.btn` showing the
current value (+ `chev_d` icon), `onclick` opening a searchMenu whose
items close over getter/setters, plus an `el._repaint = paintFn` hook
so external prefill code can update the label.

## Persistent popover - the click-pinned breakdown panel

Tooltips die on the first re-render; data the user wants to WATCH
(live token accounting under a meter) needs a popover: a fixed div on
`document.body`, opened by clicking its anchor, dismissed by clicking
anywhere outside, and - the key trick - re-anchored when re-renders
replace its anchor element:

```js
function popOpen(key, anchor) {
  if (pop?.key === key) { pop.anchor = anchor; update(); return; }
  popClose();
  pop = { key, el: h("div.ctx-pop"), anchor };
  pop.dismiss = (e) => {           // outside-click, capture phase
    if (pop.el.contains(e.target)) return;
    if (pop.anchor?.isConnected && pop.anchor.contains(e.target)) return;
    popClose();                    // anchor excluded: its own click toggles
  };
  document.body.appendChild(pop.el);
  setTimeout(() =>
    document.addEventListener("pointerdown", pop.dismiss, true), 0);
  update();
}
function update() {                // called on every data tick
  pop.el.replaceChildren(...content());
  const r = pop.anchor.getBoundingClientRect(), w = pop.el.offsetWidth;
  pop.el.style.left = Math.max(6, Math.min(r.left, innerWidth - w - 6)) + "px";
  pop.el.style.bottom = (innerHeight - r.top + 6) + "px";   // above anchor
}
```

The anchor widget's own click handler toggles (`open? close :
open`) - the dismiss handler must EXCLUDE the anchor or the toggle
closes and instantly reopens. When a re-render rebuilds the anchor,
the new element checks (in a `setTimeout(0)`, once connected) whether
a popover for its key is open and re-points `pop.anchor` at itself,
then refreshes - the panel survives stream-driven rebuilds and its
numbers move live. Anchoring by `bottom:` (not top) keeps growth
upward for composer-adjacent widgets.

The meter itself: `div.ctx-meter` with a 90×4px track and an `<i>`
fill sized by `width:%`; > 80% adds `.warn`. When the maximum is
unknown, show `~12.3k / ?` - an honest "?" beats a fabricated bar.

## Modal system

Deliberately NOT a stack - one modal at a time in a dedicated
`#modal-root`; opening a modal closes the previous one. Flows that
feel nested (wizard → sub-dialog → wizard) are re-entry: the
sub-dialog's `onClose` reopens the parent at the right step.

```js
let onCloseCb = null, sticky = false;
function openModal(content, opts) {
  closeModal();
  const bd = h("div.modal-backdrop",
    { onclick: (e) => { if (e.target === bd && !opts?.sticky) closeModal(); } },
    content);                       // e.target check: clicks INSIDE pass
  root.appendChild(bd);
  onCloseCb = opts?.onClose || null;
  sticky = !!opts?.sticky;
  const first = content.querySelector("input, textarea, select, button");
  if (first) setTimeout(() => first.focus(), 30);
  return bd;
}
function closeModal() {
  root.replaceChildren();
  sticky = false;
  const fn = onCloseCb;
  onCloseCb = null;                 // clear BEFORE calling - onClose
  if (fn) fn();                     // may open another modal
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!sticky) closeModal(); closeMenu(); }
});
```

Rules encoded there: `onClose` fires exactly once however the modal
ends (Cancel, X, backdrop, Escape, success) and is nulled before
invocation so it may itself open a modal; `sticky` modals ignore both
backdrop clicks and Escape - reserved for mandatory flows (first-run
wizard, credential prompts) and irreversible decisions (bulk delete)
that must not be dismissible by accident; Escape also closes any open
menu; the first form control is auto-focused after ~30 ms (the modal
must be laid out before focus works).

`modalShell(title, bodyNodes, footNodes, wide)` is the standard
frame: head (title + spacer + X icon-button), scrolling body
(`.modal-body`, children `flex:none` so content scrolls rather than
shrinks), optional right-aligned footer. The backdrop centers with
`padding-top: 9vh`; the panel is 560px (760 for `.wide`), max-height
82vh.

Recipes, all thin compositions over the manager:

- **Confirm**: never `window.confirm`/`alert`. `confirmModal(title,
  message, label, onConfirm, {danger})` - Cancel + a danger (default)
  or primary confirm button that closes THEN acts.
- **Typed confirmation** for destruction: the confirm button starts
  `disabled` and an `input` listener enables it only when the typed
  text equals the resource name exactly.
- **Prompt/name dialogs**: an input plus Enter-to-submit wiring -
  `input.addEventListener("keydown", e => { if (e.key === "Enter")
  saveBtn.click(); })` - so keyboard and button share one path.
- **Credential prompt queue**: backend auth events push into an
  array; one sticky modal shows at a time; answer or cancel shifts
  the next. Password input, Enter submits, cancel aborts the blocked
  operation with an explanatory toast.
- **Wizard**: step index + one `paint()` that redraws dots, body, and
  the Back/Next buttons per step; each step function reconfigures
  `nextBtn` (label, disabled, onclick). Opened sticky with no X.
- **Streaming-log modal**: a `pre.term` that appends lines from
  events and pins `scrollTop = scrollHeight`, a status chip swapped
  by state, and a "Run in background" close button.

## Command palette

A modal-hosted fuzzy jumper (Ctrl+K): borderless input on top, result
list below, `div.pal-item` rows with icon, label, and a dim
right-aligned kind column. Instant results come from in-memory names
(substring matches rank before fuzzy-subsequence matches); content
search hits arrive from a 220 ms-debounced backend query and are
ignored if the input has moved on (compare the query the answer was
FOR against the current value), then dedup by href against the label
hits. ArrowUp/Down + Enter navigate; clicking or Enter closes the
modal and navigates. Cap the merged list (~18) - a palette is for
jumping, not browsing.

## Small controls

**Chips**: `span.chip` pill (11px, 20px radius) + modifier class for
meaning (`.ok .warn .err .acc`) - modifiers swap color and tinted
background only. Chips freely embed icons and dots.

**Dots**: 7px circles; `.run` and `.attn` pulse via a shared opacity
keyframe (1.6 s calm, 0.9 s urgent), `.ok/.err/.idle` are static.
The three-state liveness cue everywhere: pulsing = alive, static =
settled, stopped counter = stuck.

**Badge count**: absolutely positioned pill (`top:-3px; right:-4px`,
min-width 14px) on a `position:relative` icon button.

**Tabs**: `div.tabs` underline row; each `button.tab` carries its key
in `dataset`, the switcher toggles `.active` (2px accent underline)
and swaps a pane container's children. Count badges are `span.cnt`.

**Segmented control**: a bordered `div.seg` of flat buttons. The
control repaints ITSELF on click (each button toggles `.on` across
the set, then fires `onchange`) - it must stay correct inside a modal
the parent view won't re-render. The "inherit" member renders italic
and reports the inherited value in its tooltip.

**Toggle switch**: a real `<input type=checkbox>` visually hidden
inside `label.switch`, with a `.track` div whose `::after` knob
slides 14px via `input:checked + .track::after { transform:
translateX(14px) }`. Native checkbox semantics (focus, label click,
forms) for free; CSS does all drawing.

**Liveness ellipsis**: `.think-dots::after` animates `content`
through "", ".", "..", "..." on a 1.2 s step animation - attach it
after any live counter or "composing" label.

## Collapsible result cards

The pattern for tool calls / long operations in a feed. A card =
head row + optional preview + body:

- The head always tells the whole story collapsed: icon, name, a
  target chip that is NEVER truncated, an ellipsized summary, compact
  stats (`+12 −3` for diffs, "14 lines" for output, a live token
  estimate while running), and a state chip.
- FINISHED cards collapse to the head; clicking toggles `.hidden` on
  the body, rotates a chevron (`.tc-chev.open { transform:
  rotate(90deg) }`), and updates the head's `data-tip`
  ("Click to expand"/"collapse"). IN-FLIGHT cards (running, awaiting
  a decision) are forced open - an approval needs its parameters and
  diff visible; reduced visibility must never hide a decision or a
  failure.
- Collapsed cards keep a CONTENT PREVIEW: the first 3-4 lines of the
  output or diff behind a bottom fade (`::after` gradient) with a
  down-chevron hint; clicking it expands.
- Long finished output collapses behind a toggle button ("Show all N
  lines" ↔ "Show less") that swaps `pre.textContent` - beyond ~16
  lines a result must not flood the feed.
- Live streaming output pins its `<pre>` to the bottom
  (`scrollTop = scrollHeight` in a `setTimeout(0)` after append).
- Open/closed state is stored ON THE DATA (`msg._open`), not the DOM
  - the feed is rebuilt on every render and the DOM node is
  disposable.
- Ultra-low-signal successful calls render as one quiet line instead
  of a card, full output in the hover tip; pending/denied/failed
  always keep the card.

**Diff block**: `div.diff` of one `div.dl` per line, classed by
prefix - `+` → `.add`, `-` → `.del`, `@@` → `.hunk` - colored purely
in CSS. Line-based, no intra-line diffing.

## Splitters and panes

`splitHandle(pane, key)` returns a thin drag handle to place between
a fixed-width pane and its flexible neighbor. Width and collapsed
state live in a persisted store keyed by `key` so layouts survive
re-renders and restarts. Drag: on `mousedown` record `startX` and
the stored width, attach `mousemove`/`mouseup` to the DOCUMENT (the
pointer leaves the 5px handle immediately), clamp `start + dx` to
min/max (~160-480), write `pane.style.width` directly (no re-render
mid-drag), persist on release. A chevron button or double-click
toggles collapse (`width: 0` + `.pane-collapsed`); the button's click
must `stopPropagation()` so it doesn't start a drag. Hide the tooltip
on every drag start - the handle moves out from under the cursor.
The vertical variant is the same code on `clientY`/`height`, with max
computed live from the container minus the neighbors' minimum heights
so the pane can grow until the neighbor is minimal, not to an
arbitrary constant.

## Trees and drag & drop

Lazy file/folder trees render rows indented by
`padding-left: 8 + depth*14 px` with a twisty (▸/▾), fetch directory
children on first expand, cache per path, and expose imperative hooks
on the returned element (`el.refresh()` clears the cache and
re-renders) - the owner calls them after external changes. Selection
is a `.sel` class + a `selected` variable, not DOM state.

Organizer trees (drag rows into virtual folders) use native HTML5
DnD. The hard-won rules:

```js
row.draggable = true;
row.addEventListener("dragstart", (e) => {
  drag.kind = kind; drag.id = id;          // module-level live-drag state:
  e.dataTransfer.effectAllowed = "copyMove"; // dragover can't read data!
  e.dataTransfer.setData("text/plain", id);  // some engines need data set
  row.classList.add("dragging");
});
target.addEventListener("dragover", (e) => {
  if (!validDrop(targetId)) return;   // NO preventDefault → ⃠ cursor
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  // highlight target; if it's a collapsed folder, start a ~550ms
  // timer that expands it under the drag ("spring-open")
});
document.addEventListener("dragend", cleanupAll);
```

- `dragover` cannot read `DataTransfer` payloads, so the live drag is
  tracked in module state; the payload is only set because some
  engines refuse to start a drag without one.
- `effectAllowed` must cover every zone's `dropEffect` (`"copyMove"`
  when tree drops use move and another target uses copy) - a
  dropEffect outside effectAllowed makes the browser silently CANCEL
  the drop: the zone highlights, release does nothing.
- Invalid targets simply skip `preventDefault` - the not-allowed
  cursor is free. Validity: not into itself, not into its own
  subtree (walk `parentId` with a cycle guard), and dropping where
  the item already lives is invalid (a no-op should read as one).
- Cleanup lives on a DOCUMENT-level `dragend` - the source row may
  have been rebuilt mid-drag by the spring-open re-render, so its own
  dragend can be lost; sweep `.dragging` off everything.
- Container empty space doubles as the root drop target, gated on
  `e.target === container` so row targets underneath keep their own
  handling; wire the container ONCE (flag on the element) since it
  outlives re-renders.

## Editors and code display

Read-only code gets a static syntax highlighter over a plain wrap
div, with a hand-rolled fallback (a gutter div of line numbers next
to a `pre`) when highlighting throws. An embedded editor component
returns a host div with `getValue()/setValue()` and mounts the real
editor only once the host `isConnected` - poll with
`setTimeout(mount, 30)`, NOT `requestAnimationFrame`: rAF never fires
in headless/hidden windows, and the component must work inside modals
appended after creation. A JSON config editor is just a textarea +
live `JSON.parse` on every input, painting a Valid/error status line
and disabling Save while broken - validation you can see beats
validation on submit.

Clipboard: try synchronous `document.execCommand("copy")` on a
hidden textarea FIRST (permission-free); only fall back to
`navigator.clipboard.writeText` and always `Promise.race` it against
a ~1.5 s timeout - in embedded webviews the permission prompt can
leave the promise pending forever. Toast the result honestly either
way.

## Rules

- One element helper, everywhere; `null`/`false` children must
  vanish, and any `replaceChildren` wrapper must filter them too.
- Tooltips are one shared, delegated, `pointer-events:none` node fed
  by `data-tip`; measure before placing, prefer below, flip above,
  clamp both axes with a 6px margin; hide on mousedown/scroll/
  resize/blur and whenever a widget moves under the cursor.
- Every fixed overlay clamps to the viewport; searchable menus
  re-place after EVERY paint from measured size - never guess the
  flipped height.
- One menu at a time; outside-dismiss via document-capture mousedown
  attached in `setTimeout(0)`; keyboard on document capture (focus
  can be stolen at any time); accent-mark the open menu's anchor.
- Hover must never repaint the row under the cursor - mouseenter
  fires before mousedown and a rebuilt node eats the click.
- Modals: single slot, `onClose` fires once and is cleared before
  invocation, Escape/backdrop close unless sticky; sticky is for
  mandatory flows and no-undo decisions only. Never
  window.confirm/alert; destructive confirmations type the name.
- Widgets inside modals repaint themselves on interaction - the
  parent view will not re-render them.
- Expand/collapse state lives on the data, not the DOM; feeds are
  rebuilt wholesale and nodes are disposable.
- Keep in-flight/approval cards forced open; collapse only what is
  finished, and preview what you collapsed.
- Drags attach move/up to the document and always detach on up; DnD
  cleanup belongs on document dragend; `effectAllowed` must cover
  every zone's dropEffect or drops silently cancel.
- Debounce anything user-typed that hits a backend (~220-300 ms) and
  drop stale answers by comparing against the current query.
- Show "?" for unknown maxima; never fabricate a denominator.
- Mount embedded editors on an isConnected poll via setTimeout -
  rAF never fires headless.
