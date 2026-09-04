# Design language - desktop-utilitarian instrument panel

A visual system for data-heavy desktop-first web apps: dense, crisp,
muted. The app is an instrument panel, not a marketing site - every
pixel either shows state or accepts input, chrome is quiet, color is
reserved for meaning, and nothing moves unless the motion carries
information. Use it when the product is a tool someone lives in for
hours (a console, an IDE-like shell, an ops dashboard); skip it for
content sites, which want larger type, looser spacing, and scrolling
pages. The whole language is ~40 CSS custom properties plus a handful
of conventions; the reference design ships two themes (dark default,
light optional) switched by one `data-theme` attribute on `<html>`,
zero external resources, and inline SVG icons. Widgets built in this
language live in [components.md](components.md); the state/rendering
machinery that repaints them is [js-app-architecture.md](
js-app-architecture.md).

## Token sheet

Structural tokens are theme-independent and live on bare `:root`;
each theme redefines ONLY colors under `:root[data-theme="…"]`. The
HTML ships `<html data-theme="dark">` and a settings control writes
`document.documentElement.dataset.theme` - no media queries, the user
picks (user in control beats OS guessing).

```css
:root {
  --font-ui: "Inter", "Segoe UI", "Cantarell", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", "Cascadia Code", "Fira Code",
    ui-monospace, "SF Mono", Menlo, Consolas, monospace;

  --fs-xs: 11px;   /* metadata, chips, table headers, statusbar */
  --fs-sm: 12px;   /* controls, lists, most UI text */
  --fs-md: 13px;   /* body text, chat, inputs - the base size */
  --fs-lg: 15px;   /* view titles (h1) */
  --fs-xl: 18px;   /* rare; hero moments only */

  --r-sm: 3px;     /* kbd, small hit targets, tree rows */
  --r-md: 5px;     /* buttons, inputs, list rows */

  --topbar-h: 40px;
  --statusbar-h: 24px;
  --sidebar-w: 224px;
}

:root[data-theme="dark"] {
  --bg-0: #101214;   /* app chrome: topbar, sidebar, statusbar */
  --bg-1: #16191d;   /* panels, main view background */
  --bg-2: #1c2026;   /* raised: cards, inputs, list hover */
  --bg-3: #242931;   /* hover on raised, code chips */
  --bg-4: #2d333d;   /* active/pressed */

  --bd-0: #23272e;   /* subtle borders: row separators, card seams */
  --bd-1: #30363f;   /* strong borders: panel edges, controls */

  --fg-0: #e2e6ea;   /* primary text */
  --fg-1: #a8b0ba;   /* secondary */
  --fg-2: #6f7883;   /* muted: hints, timestamps, icons at rest */

  --acc: #4d9de6;    /* the one accent: actions, links, focus */
  --acc-fg: #0d1319; /* text ON accent fills */
  --acc-dim: #244a68;/* accent wash: drag targets, switch tracks */

  --ok: #4cae6e;   --ok-bg: #1a2e22;
  --warn: #d9a03c; --warn-bg: #33291a;
  --err: #d95c4d;  --err-bg: #33201d;
  --run: #4d9de6;  --acc-bg: #1a2a3a;
  --agent: #a07be0; --agent-bg: #271f38; /* machine-created things */

  --diff-add-bg: #16281c;
  --diff-del-bg: #2e1c1a;

  --sel: #26415c;    /* selected row/item fill */
  --shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
}

:root[data-theme="light"] {
  --bg-0: #ebedef; --bg-1: #f6f7f8; --bg-2: #ffffff;
  --bg-3: #eceef1; --bg-4: #e0e4e9;
  --bd-0: #d8dbdf; --bd-1: #c3c8cf;
  --fg-0: #1d2329; --fg-1: #4b545e; --fg-2: #7d868f;
  --acc: #1a6fc4; --acc-fg: #ffffff; --acc-dim: #a8ccec;
  --ok: #22863a;   --ok-bg: #e2f2e7;
  --warn: #a8700a; --warn-bg: #f6ecd9;
  --err: #c4392b;  --err-bg: #f8e4e1;
  --run: #1a6fc4;  --acc-bg: #e0edf9;
  --agent: #6f42c1; --agent-bg: #ece4f7;
  --diff-add-bg: #e2f2e7; --diff-del-bg: #f8e4e1;
  --sel: #cfe3f5;
  --shadow: 0 6px 24px rgba(20, 30, 40, 0.18);
}
```

How to read the palette: FIVE background steps form an elevation
scale - each interactive step is "one bg up" (rest bg-2, hover bg-3,
active bg-4). Dark backgrounds are cool near-blacks with a faint blue
cast, never pure #000; text tops out at #e2e6ea, never pure white
(both extremes glare on long sessions). THREE text tones carry the
whole hierarchy - resist inventing a fourth. Borders come in exactly
two weights: bd-0 separates things that belong together (rows in a
list), bd-1 separates things that don't (panel from panel, control
from canvas). Every status color has a matching `-bg` wash so a chip
can be `color: var(--warn); background: var(--warn-bg)` with no
border and still read at 11px.

The font stacks name preferred faces (Inter, JetBrains Mono) but
nothing is bundled or fetched - no `@font-face`, no CDN, no external
request of any kind. Whatever is installed wins; system-ui and
ui-monospace guarantee a good floor. Base UI size is 13px; monospace
is pinned at 12px (`code, kbd, pre, .mono`) so identifiers align in
tables regardless of context.

Spacing has no token scale - the reference design uses raw pixels on
a de-facto 2/4/6/8/10/12/14/16 ladder, with 6-8px as the workhorse
gap and 10-16px for panel padding. Radii above `--r-md` are also
literal and meaningful: 6px cards, 7-8px grouped panels, 9-10px
floating popovers, 14px the chat composer, and 20px (or 50%) means
"pill/round" - the radius itself signals the widget class.

## Z-index bands

Give overlays explicit bands with air between them; never z-index an
in-flow element above single digits.

```
1-9    in-panel risers: split handles (5), sticky tree roots (6)
40-90  panel-local popups: autocomplete (40), context menus (90)
100    modal backdrop + modal
200    toasts, full-view wipes
250    searchable dropdowns (must beat modals AND toasts)
300    tooltip, pinned popovers - nothing may cover a tooltip
400    debug/inspector overlays (top of the world)
```

## App chrome and layout

One fixed viewport, no page scroll ever: `html, body { height: 100%;
overflow: hidden }`. The skeleton is a flex column holding a flex
row:

```
#app (column, 100vh)
├─ #topbar     40px   bg-0, border-bottom bd-1
├─ #shell (row, flex:1, min-height:0)
│  ├─ #sidebar 224px  bg-0, border-right bd-1
│  └─ #view    flex:1 bg-1 - views render here
└─ #statusbar  24px   bg-0, border-top bd-1, fs-xs
```

Chrome (bg-0) is one step darker than content (bg-1), so the working
area reads as the lit surface of the instrument. The topbar splits
left/center/right (`flex: 1 1 0` wings, `flex: 0 1 380px` center
search); the statusbar is two `gap: 14px` clusters of icon+text
items. Views are themselves columns: a `.view-head` (title row,
`padding: 10px 16px`, `flex-wrap: wrap` so actions never clip on
narrow windows) over a `.view-body` (`flex: 1 1 auto; min-height: 0;
overflow-y: auto`).

Scroll containment is the load-bearing rule: every flex child on the
path to a scroller carries `min-height: 0` (or `min-width: 0`
horizontally), the scrolling element owns `overflow-y: auto`, and
children of a scrolling column are `flex: none` - otherwise flex
compresses them into clipped mush instead of letting them overflow
into scroll. Side-by-side regions use `.pane-split` (row) of `.pane`
columns; a 6px transparent `.split-handle` between them turns
`col-resize`, tints `var(--acc-dim)` on hover/drag, and can collapse
a pane to `width: 0`. Panel sizes the user drags are state - persist
them. Give the leftover-space pane `flex: 1 1 0` (basis 0, NOT auto)
or a content-heavy sibling rubber-bands the divider.

Text is not selectable by default (`body { user-select: none;
cursor: default }`) - this is an application, and drags must never
highlight labels. Content areas opt back in with a `.selectable`
class (plus inputs/textareas). Scrollbars are styled once, globally:
10px, thumb = bd-1 inset by a 2px transparent border
(`background-clip: content-box`), fg-2 on hover, transparent corner.

## Density

Data-heavy UI earns its keep in rows-per-screen. The reference
numbers:

- Buttons: `padding: 4px 10px`, 12px/500 text, `line-height: 18px`
  → ~26px tall. Small variant `2px 7px` at 11px. Icon buttons are
  24×24 (28×28 in the topbar), borderless until hover.
- Inputs/selects: `padding: 4px 8px`, 12px text - same 26px rhythm.
- List rows: `padding: 5px 8px` (sidebar items), `3px 8px` (tree
  rows), `2.5px 8px` (file tree) - 22-26px rows, radius r-md, 1px
  gaps.
- Tables: `th/td padding: 6px 10px`; headers are 11px 600 UPPERCASE
  `letter-spacing: 0.04em` fg-2 - the uppercase-microlabel treatment
  recurs for every key/label column (field labels, param keys,
  section heads).
- One line per row: `white-space: nowrap` + `text-overflow:
  ellipsis` on the growing cell (`flex: 1 1 auto; min-width: 0`);
  metadata (time, counts) is `flex: none` fs-xs fg-2 on the right.
  Sidebars never scroll horizontally - content ellipsizes.

Everything aligns with `display: flex; align-items: center; gap:
6px..8px` - gap, not margins, everywhere.

## Component states

One vocabulary for every interactive element:

- **hover** - background rises one bg step and text brightens one fg
  step (`bg-2→bg-3`, `fg-1→fg-0`); icons stay fg-2 unless active.
  Instant, no transition: hover feedback must feel wired, not eased.
- **active (current place)** - `bg-3` fill; the row's icon turns
  `var(--acc)`. Active tabs get a 2px accent edge (underline for flat
  tabs, inset top bar for editor tabs) instead of a fill.
- **selected (chosen object)** - `background: var(--sel)`, a
  desaturated accent that reads as selection without shouting.
  Keyboard highlight = sel + `inset 2px 0 0 var(--acc)` left bar;
  multi-select = sel + 1px accent outline inset.
- **focus** - inputs swap `border-color` to `var(--acc)`
  (`outline: none`); composite boxes use `:focus-within` on the
  wrapper. Focus is always visible, expressed as an accent border
  rather than a browser outline ring.
- **disabled** - `opacity: 0.45; cursor: not-allowed`. Never
  recolor; dimming preserves the label's identity.
- **pressed** - `transform: translateY(0.5px)` on `:active`; the
  only "physical" effect in the system.
- **menu-open** - the control whose dropdown is open gets a full
  accent fill (`background/border: var(--acc); color: var(--acc-fg)`)
  so there is never a question which menu belongs to what.
- **drag & drop** - dragged element `opacity: 0.45`; valid target
  `var(--acc-dim)` fill + `inset 0 0 0 1px var(--acc)` ring; reorder
  landing slot = 2-3px accent bar on the receiving edge; drop ZONES
  (whole panels) use `outline: 2px dashed var(--acc); outline-offset:
  -4px`.

Semantic colors always mean the same thing: `--ok` success/safe,
`--warn` needs-attention/pending/unsaved (a dirty file dot, a
pending approval card, a read-write badge), `--err`
failure/destructive, `--run` in-progress (blue, same hue as acc),
`--agent` (purple) marks anything a machine created - its branches,
its messages, its pills - so human and automated work never blur.
Attention cards and toasts encode kind as a 3px LEFT border in the
status color on a neutral bg-2 card; chips encode it as fg+wash.
Live-ness is a 7px `.dot` pulsing opacity 1→0.35 (1.6s, or 0.9s when
attention is demanded).

## Iconography

Inline SVG only - one JS dict of 16×16 `viewBox="0 0 16 16"`
`fill="currentColor"` paths (octicon-style filled outlines, 0.75px
corner radii), injected as `<span class="ico">` (14×14 default,
11-12px in dense metadata, 18px `.ico-lg`) or hydrated into static
markup via `data-ico` attributes. No icon font, no sprite requests,
no emoji in chrome. `currentColor` means icons inherit state color
for free: fg-2 at rest, fg-0 on hover, acc when active. Decorative
glyph characters are allowed in tiny roles (● dirty dot, ↗ external
link, × clear) where an SVG would be ceremony.

## Tooltips and floating surfaces

Native `title=` is banned - it can't be styled and ignores the app's
layout. One shared `#tooltip` div serves the whole app: elements opt
in with `data-tip="text"` (plus optional `data-tip-kbd`), a
delegated listener shows after 350ms, and placement measures the
node, prefers below-center, flips above if it would clip, and clamps
into the viewport with a 6px margin - a tooltip is NEVER cut off by
a window edge. It hides on mousedown, scroll, resize, and blur. A
`.card` variant carries structured HTML (title, key/value grid) for
rich hover inspection; still pointer-inert (`pointer-events: none`).
Menus, popovers, and palettes follow the same physics: `position:
fixed`, appended to `document.body` (so panel re-renders can't
destroy them), bg-2, 1px bd-1, radius 6-9px, `var(--shadow)`,
clamped to the viewport, dismissed by any outside pointerdown.
Modals: fixed backdrop `rgba(6,8,10,0.6)`, panel bg-1, radius 8px,
`padding-top: 9vh` (top-anchored, not centered), `max-height: 82vh`
with head/body/foot rows where only the body scrolls.

## Motion

Motion is scarce and semantic. The complete budget:

- Micro-transitions: `0.12s` (ease default) on chevron rotation
  (90° open), switch thumbs, focus border-color. Nothing else on
  ordinary controls - hover/active state changes are instant.
- Entrances: toasts slide up 6px + fade in `0.15s ease-out`.
- Liveness loops: the `pulse` opacity keyframe on status dots and
  streaming cursors; a stepped "…" typing animation (`steps(1)`
  cycling `content: "" . .. ...` at 1.2s) for anything waiting on a
  machine; a spinner only inside full-view transitions.
- Attention flashes: found-in-page items get a 1.4s decaying accent
  outline; updated rows a 1.6s background fade - self-removing, so
  a glance later the UI is still.
- Choreography (rare, one per app): big handoffs may use the View
  Transitions API - shared elements morph over `0.4s
  cubic-bezier(0.22, 0.9, 0.3, 1)` while the page cross-fades in
  0.22s underneath, secondary controls delayed 0.05s. Guarded: if
  the API is missing or names collide, it degrades to a hard cut.

Deliberately NOT animated: hover states, panel resizing, list
reorder, scroll, collapse/expand of rows, theme switching. Movement
that merely decorates a state change slows the operator down.

## Empty states and long operations

An empty region says so in words, centered and quiet: flex-centered
column, `gap: 8px`, fg-2 text, a 28px icon tinted `var(--bd-1)`
(dimmer than text - the icon is texture, not content), `padding:
48px 16px`. The copy tells the user what would fill the space and
how ("No agents yet - press + to start one"), styled `white-space:
normal` even where siblings are nowrap. There are no skeleton
screens: local-first data arrives fast enough that skeletons would
be theater; long operations get a live monospace log (a `.term`
block: fixed near-black `#0a0c0e` background in BOTH themes, 11.5px
mono, `white-space: pre-wrap`) or a progress row with a pulsing dot.

## Accessibility notes

Contrast is engineered into the token pairs: fg-0 on bg-0..2 and
every status fg on its `-bg` wash clear 4.5:1 in both themes; fg-2
is the floor and is reserved for text whose loss is tolerable
(hints, timestamps). Focus is always visible via accent borders/
fills rather than default outlines. Hit targets stay ≥ 24px on one
axis even in dense rows (row height + full-width click area).
Keyboard paths shadow every pointer path - palette (Ctrl+K), list
focus + arrow navigation, Enter/Escape - and shortcut hints ride in
tooltips (`data-tip-kbd`) and `<kbd>` chips (bg-3, bd-1 with a 2px
bottom border for the keycap look). Decorative SVG carries
`aria-hidden="true"`. The system does not honor
`prefers-reduced-motion` because there is almost no motion to
reduce; add the media query if you adopt the choreography tier.

## Porting the language

To reskin for a new identity, change ONLY: `--acc`/`--acc-fg`/
`--acc-dim` (+ `--run` if it mirrors acc), the two font stack
preferences, and optionally the bg ramp's temperature (keep five
steps, keep the deltas subtle). Domain colors (`--agent` here) are
per-app: add one fg+bg pair per concept that must be recognizable at
a glance, and stop before six. Keep everything else - the fg/bd
ladders, status colors, sizes, radii, density numbers, z-bands, and
state conventions are the language; swapping them is a redesign, not
a rebrand. New chrome dimensions (`--topbar-h` etc.) are honest
knobs. Add themes by adding one more `:root[data-theme="…"]` block
that redefines every color token - components never hardcode a
color, so themes are complete by construction.

## Rules

- Never hardcode a color in a component - every color is a token
  reference; a hex in views CSS is a bug (the rare exceptions:
  terminal blacks and shadow rgba).
- Interactive fills move exactly one bg step; text moves one fg
  step. Two-step jumps read as a different widget.
- One accent. Blue means "you can act here"; if everything is blue,
  nothing is.
- `--sel` for selection, bg-3 for "current location", accent fill
  only for menu-open - don't blur the three.
- fs ladder is five sizes; introduce no 14px or 16px "just this
  once".
- Every scroll container needs `min-height: 0` up its flex chain and
  `flex: none` children; every nowrap row needs `min-width: 0` on
  the ellipsizing cell. Most layout bugs are one of these two.
- Fixed-position anything (tooltip, menu, toast, palette) clamps to
  the viewport and dies on outside pointerdown/scroll/resize.
- No external resources at all - no CDN scripts, webfonts, or remote
  images; the app must render identically air-gapped.
- Status colors are a contract; never use `--err` for emphasis or
  `--warn` decoratively.
- Hover is instant; only chevrons, switches, and focus borders get
  the 0.12s ease; looping animation is reserved for genuinely live
  things and must self-remove when done.
- Uppercase 11px/600 letter-spaced fg-2 is the ONLY label treatment
  for keys/columns/sections - one microformat, recognized
  everywhere.
- Dense by default, but escape hatches per row: reading surfaces
  (markdown, docs) get `max-width: 820-920px`, `line-height: 1.5+`,
  and `.selectable`.
