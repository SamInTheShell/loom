# Debugging a pywebview app

Day-to-day workflows for a pywebview app whose backend is plain Python and
whose frontend is a single `index.html` with no build step. Assumes the
app-skeleton.md setup: `file://` frontend, local DevTools, a `--debug`
flag, `uv` for running (see ../uv.md).

## Running

```bash
uv run your-app             # normal launch (the [project.scripts] entry)
uv run your-app --debug     # auto-opens Chromium DevTools attached to the window
```

In-app: `Ctrl/Cmd+Shift+I` opens DevTools any time.

## Debugging Python

**Quick smoke test of a module via `uv run python -c`.** Drop into a temp
dir, call the public entrypoints, print results. No fixtures, no
framework - just check the shapes:

```bash
uv run python -c "
from src import tools
import tempfile, pathlib
with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d)
    (root/'a.txt').write_text('hi')
    (root/'sub').mkdir()
    print('preview:', tools.preview_call('move', {'from':'a.txt','to':'sub/b.txt'}, str(root)))
    print('apply:  ', tools.call('move',         {'from':'a.txt','to':'sub/b.txt'}, str(root)))
"
```

If backend helpers wrap exceptions into a result shape (e.g.
`{ok, error, error_kind}`), smoke tests see exactly what the frontend
will see. For anything that mutates the app's config directory, point it
at a temp HOME or a config-dir override - don't pollute your real config.

**Live backend inspection via DevTools.** Once the app is running,
anything exposed on `JsApi` is callable from the JS console as
`window.pywebview.api.<method>(...)`. You can drive the Python side from
the DevTools console without restarting the app.

**Verify JsApi surface before wiring up buttons** - import the class and
check the methods exist (snippet in js-api-bridge.md). Every method the
frontend calls must be explicitly defined on `JsApi`.

## Debugging JavaScript

With a no-build frontend, what you see in `index.html` is what runs.

**Make globals intentional.** Keeping state, the API wrapper, and render
helpers as plain top-level globals means DevTools can inspect and mutate
everything live:

```js
// Inspect / mutate live state
st.projectDir
st.autoAcceptEdits = true

// Exercise backend calls through the same path the app uses
await appApi.call('tool_preview', 'move', { from: 'a.txt', to: 'b.txt' })

// Call a render helper directly to eyeball a UI card without the full flow
renderCardBody({ kind: 'move', from: 'a', to: 'b' })
```

**Pure-UI work without pywebview.** Open `src/frontend/index.html` in a
regular Chromium/Firefox to iterate on layout, CSS, and pure-JS paths.
`window.pywebview` is undefined there, so give your API wrapper a `has()`
check and let calls short-circuit to a synthetic error - fine for styling
work, useless for anything touching the backend.

**Reload after edits.** pywebview does not hot-reload. After editing
`index.html`, restart the app (`Ctrl+C`, then `uv run your-app --debug`).
DevTools' "Empty Cache and Hard Reload" works for assets, but the page
itself needs the restart because pywebview loads it as a `file://` URL
once.

## Validating a new backend-exposed tool end-to-end

A feature that spans backend, permissions, and frontend fails silently
when a layer is missed, so walk the layers:

1. **Python contract** - call the backend functions from
   `uv run python -c` against a temp dir. Cover the happy path and every
   refusal branch you wrote (sandbox escape, ignored path, bad args).
2. **Bridge** - start with `--debug`, open DevTools, call the method via
   `window.pywebview.api.*` and confirm the returned shape matches what
   the UI rendering code expects.
3. **UI** - render a fake result card straight from the console to eyeball
   the layout without involving the backend.
4. **End-to-end** - trigger the real flow (for LLM tools: point at a small
   local model, watch the request payload in the Network tab, the
   permission prompt, and the result landing in app state).

## Things that bite

- **JsApi methods must be explicit; JS calls are async.** The two classic
  bridge failures - details in js-api-bridge.md.
- **Persist paths in tilde form** (`~/foo`), expand at the filesystem
  boundary. Filesystem ops use absolute paths; storage and UI use `~`.
- **Config read per call vs at startup** - know which your app does. A
  filter or setting loaded per call picks up on-disk edits without a
  restart; one loaded at startup does not.
- **`--disable-web-security` on the embedded Chromium** is sometimes set
  so the `file://` page can call local servers (LM Studio, Ollama) that
  send no CORS headers. Only acceptable because the page is local and not
  user-navigable - never load remote URLs into such a webview.
- **DevTools opens in a sibling Qt window** via `setDevToolsPage`, not the
  upstream `debug=True` appspot redirect - see devtools.md if it breaks.
