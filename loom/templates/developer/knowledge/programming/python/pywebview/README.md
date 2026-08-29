# pywebview (Qt backend)

Building desktop GUI apps in Python with pywebview: an HTML/CSS/JS
frontend rendered by QtWebEngine's Chromium, talking to plain Python
through a bridge. The opinionated baseline throughout these notes: load
the frontend straight off the filesystem via `file://` (no helper HTTP
server), qt backend, fully local DevTools.

Read in this order:

- **app-skeleton.md** — project layout, dependencies, the complete
  minimal `app.py`, why `file://` matters, and the new-project checklist.
- **js-api-bridge.md** — the `JsApi` class: how Python methods become
  `window.pywebview.api.*` calls, async semantics, and the classic
  mistakes ("is not a function", un-awaited Promises).
- **frameless-windows.md** — the borderless-window field guide: window
  setup, compositor-native drag/resize, a page-drawn titlebar whose
  buttons and double-click work, and modals whose click-blocking
  backdrop leaves the titlebar functional.
- **devtools.md** — wiring up classic local Chromium DevTools with
  `setDevToolsPage` instead of the appspot remote-debugging redirect,
  plus its troubleshooting list.
- **debugging.md** — day-to-day workflows: smoke-testing backend modules
  with `uv run python -c`, driving the Python side from the DevTools
  console, pure-UI iteration in a normal browser, and reload behavior.
