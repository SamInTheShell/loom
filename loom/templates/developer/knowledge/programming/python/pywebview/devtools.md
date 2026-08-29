# Local Chromium DevTools in pywebview (Qt backend)

How to wire up classic, local-only Chromium DevTools — the same panel you
get from `Ctrl+Shift+I` in a normal browser — for a pywebview app, and how
to trigger it from inside the page with a hotkey.

This is **not** what `webview.start(gui='qt', debug=True)` does. That
switches on `QTWEBENGINE_REMOTE_DEBUGGING`, which exposes a
remote-debugging endpoint whose inspector frontend redirects to
`chrome-devtools-frontend.appspot.com`. Offline, or under a no-third-party
security model, that's useless. What you want is the bundled DevTools that
ships inside QtWebEngine's Chromium, attached as a sibling Qt window. No
HTTP, no appspot, no redirect.

## How it actually works

QtWebEngine exposes `QWebEnginePage.setDevToolsPage(otherPage)`. Hand it a
second page and Chromium loads the local `devtools://` inspector UI into
that page, wired to the inspected page over an internal channel. Render
the second page in a normal `QWebEngineView` and that view *is* the
DevTools window. No remote debugging port involved.

pywebview's qt backend keeps its `BrowserView` wrappers in
`webview.platforms.qt.BrowserView.instances`, keyed by `window.uid`. From
that wrapper, `bv.webview.page()` returns the underlying `QWebEnginePage`
— the page you want to inspect.

The only annoying bit is timing: `webview.start()` is blocking, the
`BrowserView` doesn't exist until the window has been created on the Qt
event loop, and the page isn't ready until `loaded` fires. Defer the
attach with `QTimer.singleShot(0, QApplication.instance(), attach)` or
hang it off `window.events.loaded`.

## Minimal hello-world

```python
# hello.py — local DevTools, no appspot redirect, Ctrl+Shift+I from the page.
import webview
from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QApplication
from qtpy.QtWebEngineWidgets import QWebEngineView

HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Hello</title>
<h1>Hello, DevTools</h1>
<p>Press <kbd>Ctrl/Cmd+Shift+I</kbd> to open local DevTools.</p>
<script>
  // Forward the hotkey to Python. The webview itself doesn't bind
  // F12 / Ctrl+Shift+I unless you set debug=True (which we don't).
  document.addEventListener('keydown', (e) => {
    if ((e.key === 'i' || e.key === 'I') && (e.ctrlKey || e.metaKey) && e.shiftKey && !e.altKey) {
      e.preventDefault();
      window.pywebview.api.open_devtools();
    }
  }, true);
</script>
"""


def open_local_devtools(window):
    """Attach Chromium's bundled DevTools to `window` in a sibling Qt window."""
    from webview.platforms.qt import BrowserView

    def attach():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        existing = getattr(bv, "_devtools_view", None)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        view = QWebEngineView()
        view.setWindowTitle(f"DevTools - {window.title}")
        view.resize(1000, 700)
        bv.webview.page().setDevToolsPage(view.page())
        view.show()
        bv._devtools_view = view  # keep a ref so it isn't GC'd

    # Defer onto the Qt event loop so BrowserView.instances is populated.
    QTimer.singleShot(0, QApplication.instance(), attach)


class Api:
    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def open_devtools(self):
        if self._window is None:
            return False
        open_local_devtools(self._window)
        return True


def main():
    api = Api()
    window = webview.create_window("Hello", html=HTML, js_api=api)
    api.bind(window)
    # NOTE: no debug=True — that's the remote-debugging / appspot path.
    webview.start(gui="qt")


if __name__ == "__main__":
    main()
```

### Why a JS hotkey instead of a Python `QShortcut`

You can install a `QShortcut` on the view, but the inner page swallows
most key events before Qt's shortcut machinery sees them. Catching the
keystroke in JS with a capture-phase listener
(`addEventListener('keydown', fn, true)`) and bouncing it through the
`js_api` bridge is the path of least resistance. The `QShortcut` route
works too if you set `shortcut.setContext(Qt.ApplicationShortcut)` and
install it via `create_window_trigger` (see app-skeleton.md).

## Auto-opening on startup

```python
window.events.loaded += lambda: open_local_devtools(window)
```

## Opening straight to the Console panel

Chromium DevTools persists "the last panel I had open" in its own
`localStorage` under `panel-selectedTab`. There is no public Qt API to
pick a panel, but you can preset that key on the DevTools page right
after it loads, then force a reload so it applies to the current session
too — see the `pin_panel` hook in app-skeleton.md's skeleton.

Notes:

- First open on a fresh profile briefly shows Elements before the reload
  swaps in Console; after that the preference sticks with no flicker.
- `panel-selectedTab` is an internal DevTools key, stable across recent
  Chromium versions but not contractually guaranteed. If a future
  QtWebEngine renames it, this fails silently — DevTools just opens to
  its own default.
- Other valid values: `'elements'`, `'sources'`, `'network'`,
  `'application'`, `'performance'`.
- No-internal-keys alternative: focus the DevTools window and press Esc —
  the bottom drawer (a console) opens under whatever panel is showing.

## Common things that go wrong

- **`BrowserView.instances` is empty / `KeyError`.** You attached before
  pywebview built the Qt window. Wrap the body in
  `QTimer.singleShot(0, QApplication.instance(), attach)` (parent the
  QTimer to the Qt app, not a not-yet-existing widget) or hang the call
  off `window.events.loaded`.
- **JS button fails with "is not a function".** The `JsApi` class must
  explicitly define every method called from JS (see js-api-bridge.md).
- **JS calls return `null` instead of results.** Bridge calls are async —
  `await` them.
- **DevTools window vanishes immediately.** You didn't keep a reference
  to the `QWebEngineView`; Qt garbage-collects it. Stash it somewhere
  long-lived (e.g. on the `BrowserView`).
- **Closing DevTools kills it permanently.** `setDevToolsPage` doesn't
  recreate the view. Re-`show()` the existing view if it's already set
  instead of re-attaching (the example does this).
- **`Ctrl+Shift+I` doesn't fire.** A focused element's own keydown handler
  called `stopPropagation()`. Register your listener with the
  capture-phase third argument (`true`).
- **A page saying "Inspectable WebContents" with an appspot link.** You
  passed `debug=True` to `webview.start()`. Remove it — the
  `setDevToolsPage` route doesn't need it.
- **Importing `webview.platforms.qt` fails.** That module only loads once
  the qt backend is selected. Pin `webview.start(gui="qt")`, or import
  lazily inside the attach function (the examples do this).
