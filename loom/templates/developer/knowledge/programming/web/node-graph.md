# Node-graph visualization — interactive SVG map in plain JS

An explorable node-and-edge map — cluster topology, service mesh,
pipeline DAG — built with zero libraries: SVG elements for the scene,
CSS for node state, one transform group for pan/zoom, and short
`requestAnimationFrame` loops for transient activity pulses. Choose
SVG-in-the-DOM over `<canvas>` when the graph is tens-to-hundreds of
nodes: hit-testing, hover, tooltips, and state animation come free
from the DOM/CSS, text stays crisp at every zoom, and a full re-render
is one cheap subtree rebuild. Canvas only wins past ~1–2k moving
elements, where you pay for it by hand-rolling picking and text.

## Scene structure

One full-viewport `<svg>`, one `world` group that carries the ENTIRE
pan/zoom transform, and three child layers in paint order — edges
under pulses under nodes:

```js
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs) {
  const e = document.createElementNS(NS, tag);       // NS, not createElement
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
const world = el('g', {}), edgesL = el('g', {}),
      pulsesL = el('g', {}), nodesL = el('g', {});
world.append(edgesL, pulsesL, nodesL);
svg.appendChild(world);
```

A decorative background grid is a separate fixed `<div>` behind the
svg — `pointer-events: none`, two 1px `linear-gradient`s repeated at
44px, opacity .5. It deliberately does NOT pan: a static backdrop is
free and nobody notices. The svg itself gets `cursor: grab` (class
`grabbing` while panning) and the body `overflow: hidden;
user-select: none` so drags never select text.

## Data model and event feed

Nodes own their coordinates; edges are DERIVED from domain state at
render time, never stored:

```js
// node: { id, status: 'up'|'down'|'decom', x, y }   // x,y persisted
// state also carries whatever the edges derive from (groups, links…)
```

The renderer never mutates state. A tiny emitter decouples them:

```js
const handlers = {};
function on(evt, fn) { (handlers[evt] = handlers[evt] || []).push(fn); }
function emit(evt, d) {
  (handlers[evt] || []).forEach(fn => { try { fn(d); } catch (e) { console.error(e); } });
}
```

Two channels matter to the graph: `change` (state differs — redraw
everything) and `activity` (transient event `{ kind, segs: [[fromId,
toId], …] }` — animate pulses, no redraw). Every mutation in the state
layer ends with `emit('change')`; persistence is a debounced (~300 ms)
`localStorage.setItem(key, JSON.stringify(state))` so drag storms
don't hammer storage.

## Pan/zoom transform math

The view is three numbers. Apply them ONLY on the world group — never
touch per-element coordinates when panning:

```js
let view = { x: 0, y: 0, k: 1 };
function applyView() {
  world.setAttribute('transform',
    'translate(' + view.x + ',' + view.y + ') scale(' + view.k + ')');
}
function toWorld(cx, cy) {              // screen px -> world coords
  const r = svg.getBoundingClientRect();
  return { x: (cx - r.left - view.x) / view.k,
           y: (cy - r.top  - view.y) / view.k };
}
function zoomAt(px, py, f) {            // px,py in svg-local screen px
  const k2 = Math.max(0.3, Math.min(3, view.k * f));
  view.x = px - (px - view.x) * (k2 / view.k);   // keep cursor point fixed
  view.y = py - (py - view.y) * (k2 / view.k);
  view.k = k2; applyView();
}
svg.addEventListener('wheel', e => {
  e.preventDefault();
  const r = svg.getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 0.89);
}, { passive: false });                 // passive:false or preventDefault dies
```

Toolbar buttons call `zoomAt(centerX, centerY, 1.25 | 0.8)`. Fit
computes the node bounding box plus padding and centers it:

```js
function fit() {
  const xs = nodes.map(n => n.x), ys = nodes.map(n => n.y), pad = 90;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  const r = svg.getBoundingClientRect();
  const k = Math.min(1.4, Math.min(r.width / (maxX - minX),
                                   r.height / (maxY - minY)));
  view.k = k;
  view.x = (r.width  - (maxX + minX) * k) / 2;
  view.y = (r.height - (maxY + minY) * k) / 2;
  applyView();
}
```

Clamp k (0.3–3 here, fit caps at 1.4) or one wild wheel flick strands
the user in deep space.

## Placement

Force simulation is NOT required. Computed initial placement plus
user drag (positions persisted with the state) is calmer and cheaper:
seed N nodes on a ring starting at 12 o'clock —

```js
const a = -Math.PI / 2 + i * (2 * Math.PI / N);       // i = 0..N-1
node.x = Math.round(cx + Math.cos(a) * 250);          // rx 250
node.y = Math.round(cy + Math.sin(a) * 205);          // ry 205 (wide screens)
```

— and drop later nodes where the user right-clicked, or offset from
the last node with jitter (`x + 90 + rand*40, y - 30 + rand*80`).
If you do want an automatic untangle (dense unknown topologies), run
a bounded force relax over the same x/y fields, then STOP and let the
user own the result:

```js
for (let it = 0; it < 300; it++) {
  const fx = new Map(), fy = new Map();
  nodes.forEach(n => { fx.set(n.id, 0); fy.set(n.id, 0); });
  for (const a of nodes) for (const b of nodes) {     // repulsion
    if (a.id >= b.id) continue;
    let dx = b.x - a.x, dy = b.y - a.y;
    const d2 = Math.max(dx * dx + dy * dy, 1), f = 8000 / d2;
    const d = Math.sqrt(d2); dx /= d; dy /= d;
    fx.set(a.id, fx.get(a.id) - dx * f); fy.set(a.id, fy.get(a.id) - dy * f);
    fx.set(b.id, fx.get(b.id) + dx * f); fy.set(b.id, fy.get(b.id) + dy * f);
  }
  for (const [a, b] of edgePairs) {                   // springs, rest 160
    const A = byId(a), B = byId(b);
    const dx = B.x - A.x, dy = B.y - A.y;
    const d = Math.max(Math.hypot(dx, dy), 1), f = 0.02 * (d - 160);
    fx.set(a, fx.get(a) + dx / d * f); fy.set(a, fy.get(a) + dy / d * f);
    fx.set(b, fx.get(b) - dx / d * f); fy.set(b, fy.get(b) - dy / d * f);
  }
  nodes.forEach(n => {                                // damped step
    n.x += Math.max(-8, Math.min(8, fx.get(n.id)));
    n.y += Math.max(-8, Math.min(8, fy.get(n.id)));
  });
}
```

## Rendering — coalesced full rebuild

Don't diff. On every `change`, wipe the edges and nodes layers and
rebuild them from state; coalesce bursts through one rAF gate:

```js
let renderQueued = false;
function queueRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}
on('change', queueRender);
function render() {
  edgesL.innerHTML = ''; nodesL.innerHTML = '';      // pulsesL untouched!
  hideTip();                    // hovered element may not exist anymore
  buildEdges(); nodes.forEach(n => nodesL.appendChild(drawNode(n)));
}
```

The pulse layer is never cleared by `render` — in-flight animations
survive state rebuilds. Ten mutations in one tick still cost one
rebuild; a few hundred SVG elements rebuild in well under a frame.

## Edges

Aggregate the domain's many logical links into one drawn line per
node pair, keyed by the sorted id pair:

```js
const pairMap = new Map();
function addPair(aId, bId, label, isMeta) {
  const key = [aId, bId].sort().join('|');
  let p = pairMap.get(key);
  if (!p) { p = { a: aId, b: bId, labels: [], meta: false }; pairMap.set(key, p); }
  if (isMeta) p.meta = true; else p.labels.push(label);
}
```

Each pair renders a thin visible `<line x1 y1 x2 y2>` (stroke-width
1.2; dashed variant per class for special links) PLUS an invisible
fat twin for hover — a 1px line is unhoverable:

```css
.edge     { stroke: #27334f; stroke-width: 1.2; }
.edge-hit { stroke: transparent; stroke-width: 12; fill: none; }
```

The hit twin carries `pointerenter/move/leave` and drives an HTML
tooltip — a `position: fixed` div appended to `document.body`
(HTML wraps and styles better than SVG `<text>`), offset from the
cursor and clamped to the viewport:

```js
tip.style.left = Math.min(x + 14, innerWidth  - 280) + 'px';
tip.style.top  = Math.min(y + 12, innerHeight -  90) + 'px';
```

Hover-highlighting a node's peers re-appends bright copies of EXACTLY
the drawn links touching it (class `edge-hot`) — never invent
highlight edges the map doesn't draw, or you telegraph traffic that
doesn't exist.

## Nodes

Each node is one `<g>` positioned by its own transform, status
expressed as a class so CSS owns all state styling:

```js
function drawNode(nd) {
  const g = el('g', { class: 'gnode st-' + nd.status,
    transform: 'translate(' + nd.x + ',' + nd.y + ')' });
  g.dataset.id = nd.id;                       // hit-testing hook
  g.appendChild(el('circle', { r: 27, class: 'node-shape' }));
  g.appendChild(el('circle', { r: 33, class: 'node-ring' }));
  g.appendChild(txt(icon(nd), 0, 1, 'node-icon'));
  g.appendChild(txt(nd.id, 0, 48, 'node-label'));
  g.appendChild(txt(subline(nd), 0, 62, 'node-sub'));
  return g;
}
function txt(s, x, y, cls) {
  const t = el('text', { x, y, class: cls, 'text-anchor': 'middle' });
  t.textContent = s; return t;
}
```

State transitions are pure CSS — rebuilding with a different class is
the whole animation system for standing states:

```css
.node-ring { fill: none; stroke: #52d28e; stroke-width: 2; }
.st-down  .node-ring { stroke: #ff6b6b; stroke-dasharray: 5 4; }
.st-decom .node-ring { stroke: #ffb347; stroke-dasharray: 8 4;
                       animation: spinring 3s linear infinite; }
@keyframes spinring { to { stroke-dashoffset: -24; } }  /* rotating dashes */
.gnode:hover .node-shape { stroke: #5ab0ff; }
```

Give every text child `pointer-events: none` so the circles are the
hit target. Badges (leader star, role diamond) are extra `<text>`
children at corner offsets like `(26,-24)`; an SVG `<title>` child
inside a badge yields a free native tooltip.

## Interaction — drag vs pan vs click

One `pointerdown` on the svg dispatches everything. No manual
hit-testing: the browser did it — walk up from `e.target`:

```js
svg.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  const g = e.target.closest('.gnode');
  if (g) dragNode(g.dataset.id, e); else panView(e);
});
```

Both gestures attach `pointermove`/`pointerup` to `window` (not the
svg — the pointer leaves it mid-drag) and detach on up. Pan stores
the grab offset in screen space; node drag works in WORLD space via
`toWorld` and keeps the grab offset so the node doesn't jump to the
cursor. Click and drag share the button: a drag only "starts" after
the pointer moves 4 world-units, and pointerup without movement IS
the click (open inspector):

```js
function dragNode(id, e) {
  const nd = byId(id), start = toWorld(e.clientX, e.clientY);
  const ox = nd.x - start.x, oy = nd.y - start.y;
  let moved = false;
  const move = ev => {
    const p = toWorld(ev.clientX, ev.clientY);
    if (!moved && Math.hypot(p.x - start.x, p.y - start.y) < 4) return;
    moved = true;
    setNodePos(id, Math.round(p.x + ox), Math.round(p.y + oy)); // persists
    queueRender();
  };
  const up = () => { window.removeEventListener('pointermove', move);
                     window.removeEventListener('pointerup', up);
                     if (!moved) onInspect(id); };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}
```

Right-click builds a context menu the same way: `contextmenu` +
`preventDefault`, `closest('.gnode')` decides node actions vs
background actions ("add node HERE" uses `toWorld(e.clientX,
e.clientY)` so the node lands under the cursor). The menu is a fixed
HTML div clamped to the viewport, removed by any `pointerdown`
outside `.ctx-menu`.

## Activity pulses — animating data movement

Discrete events (write replication, election, chunk repair) animate
as pulses: for each `[from, to]` segment, a colored line plus a dot
that lerps between the two node positions over ~520 ms, then an
expanding ring "flash" at the destination. Segments of one event
stagger by 160 ms so a fan-out reads as a sequence. Color comes from
a kind→color table (`put` blue, `delete` red, `elect` orange, …).

```js
function pulse(A, B, color) {
  if (A.id === B.id) { flash(A, color); return; }     // self-event
  const line = el('line', { x1: A.x, y1: A.y, x2: B.x, y2: B.y,
    class: 'pulse-line', stroke: color });
  const dot = el('circle', { r: 5, fill: color });
  pulsesL.append(line, dot);
  const t0 = performance.now(), dur = 520;
  (function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    dot.setAttribute('cx', A.x + (B.x - A.x) * p);
    dot.setAttribute('cy', A.y + (B.y - A.y) * p);
    line.setAttribute('opacity', 0.65 * (1 - p * 0.6));
    if (p < 1) requestAnimationFrame(step);
    else { flash(B, color); line.remove(); dot.remove(); }
  })(t0);
}
function flash(N, color) {                            // arrival ring
  const c = el('circle', { cx: N.x, cy: N.y, r: 30, fill: 'none',
    stroke: color, 'stroke-width': 2.5, opacity: 0.8 });
  pulsesL.appendChild(c);
  const t0 = performance.now();
  (function step(t) {
    const p = Math.min(1, (t - t0) / 420);
    c.setAttribute('r', 30 + p * 22);
    c.setAttribute('opacity', 0.8 * (1 - p));
    if (p < 1) requestAnimationFrame(step); else c.remove();
  })(t0);
}
```

Pulses use WORLD coordinates and live inside the world group, so
pan/zoom applies to them for free. They sample node positions at
launch; a node dragged mid-flight leaves a half-second stale pulse —
fine, don't chase it.

## Driving the graph from live data

The graph is a pure subscriber. The state layer (simulation, poller,
websocket mirror — anything) mutates a single state object, then
emits; the graph's only obligations are `on('change', queueRender)`
and `on('activity', animate)`. This buys two big features cheaply:

- **Tick loop**: a `setInterval(tick, TICK_MS / speed)` advances the
  world (elections, repairs, TTL expiry) and each mutating step emits
  its own activity — the map narrates itself. Rebuild the interval to
  change speed; a `paused` flag skips it.
- **DVR time travel**: because state is one JSON-serializable object,
  `snapshot() = JSON.stringify(S)` per tick into an array gives a
  scrubbable timeline. Scrubbing sets a `frozen` message and pauses
  the ticker; every mutating entry point is wrapped once —
  `const guard = fn => (...a) => frozenCheck() || fn(...a)` — so a
  frozen world REFUSES writes with a human explanation instead of
  silently forking history. `restore(json, {persist:false})` while
  reviewing; restore the saved live-edge snapshot (persist true) to
  resume. Exclude session-scoped fields (open connections, watchers)
  from snapshots and re-attach them on restore.

Auxiliary chrome follows the same subscription: an inspector window
re-renders its HTML on every `change` (preserving `scrollTop` across
the swap), a health chip recomputes its class/tooltip, floating
windows are keyed by id so re-opening focuses instead of duplicating.
Keep window/terminal code out of the graph module; the graph exposes
only `init(svg, onInspect)`, `fit()`, `zoom(f)`, `render()`.

## Rules

- All SVG elements via `createElementNS('http://www.w3.org/2000/svg',
  …)` — `createElement('svg')` and friends silently render nothing.
- Pan/zoom is ONE transform on the world group. Never loop over
  elements updating x/y for a pan; that's the canvas tax without
  canvas.
- Wheel handler needs `{ passive: false }` or `preventDefault()` is
  ignored and the page scrolls under the map.
- Attach drag `pointermove/up` to `window`, always remove both on up;
  listeners left behind are the classic stuck-drag bug.
- Full rebuild + rAF coalescing beats diffing at this scale; but keep
  transient animation elements in their OWN layer that render never
  clears, and hide any tooltip on rebuild (its anchor may be gone).
- 1px lines are unhoverable — pair every visible edge with a
  transparent 12px hit twin.
- Text inside nodes gets `pointer-events: none`; the shape is the hit
  target, `closest('.gnode') + dataset.id` is the whole hit test.
- Distinguish click from drag with a movement threshold (~4 units),
  not with timers.
- Clamp zoom (~0.3–3) and clamp every fixed-position popup (tooltip,
  context menu, toast) to the viewport.
- HTML overlays for tooltips/menus/windows, SVG for the scene — each
  layer does what it's good at; give overlays explicit z-index bands.
- Persist node positions with the data (debounced) — a layout the
  user arranged is state, and losing it on refresh reads as a bug.
- Standing states are CSS classes (dashes, keyframes); rAF loops are
  only for transient, self-removing elements.
