# Web frontend knowledge

Browser-side techniques in plain HTML/CSS/JS — no frameworks, no
build steps, no external libraries or CDNs, so everything here works
offline, inside a `file://` page, or embedded in a desktop webview
(see `../python/pywebview/`). Server-side HTML belongs with its
language (Go: `../go/webapps/`).

Desktop-first app UI (the three files are layers of one system —
design vocabulary, widget library, and the machinery underneath):

- **design-language.md** — the desktop-utilitarian design language:
  the full token sheet (color ramps, semantic status colors, type
  scale, spacing, radii, z-bands), two-theme model, density rules, a
  scarce motion budget, and which knobs to turn when rebranding.
- **components.md** — the framework-free widget catalog: the `h()`
  element helper, viewport-clamped tooltips (with the exact math),
  menus, search palettes, modals, tool cards, splitters, trees,
  toggles — each with its DOM convention, mechanism, and gotchas.
- **js-app-architecture.md** — the application machinery: classic
  script tags as the module system under `file://`, the single
  mutable store and its three state tiers, full-rebuild rendering
  with render parking and in-place stream patching, hash routing,
  the backend-event fan-out, and multi-window popouts.

Standalone techniques:

- **node-graph.md** — an interactive node-and-edge visualization
  (cluster topology, service maps) in hand-written SVG: layered
  `<g>` rendering, pan/zoom transform math, computed layout plus
  drag, rAF-coalesced redraws, animated state transitions, and an
  optional force-relax pass — with the real tuning constants.

To find something: search for the topic, then read the match. To add
something: one file per topic, `# heading` first, lead with the
recommendation, and list new files here.
