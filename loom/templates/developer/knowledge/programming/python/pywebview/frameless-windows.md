# Frameless (borderless) windows - the field guide

A frameless pywebview window has NO system titlebar, border, or window
buttons: the page draws all of it. Done right it looks native and feels
native - drag, resize, snap, double-click-maximize all work - because
everything routes through the compositor instead of faking geometry
math. This guide is the complete recipe for the Qt backend.

The golden rule up front: **use the compositor-native primitives**
(`startSystemMove` / `startSystemResize`) for drag and resize. Manual
"track mouse deltas, call window.move()" implementations stutter, break
on Wayland (clients can't position themselves), and fight multi-monitor
DPI. The native calls hand the gesture to the window manager and
everything just works, X11 and Wayland alike.

## 1. Window setup

```python
window = webview.create_window(
    "MyApp",
    url=INDEX_HTML.as_uri(),
    js_api=api,
    width=1280, height=820,
    min_size=(940, 600),        # the WM enforces this even frameless
    frameless=True,             # no system chrome - the page draws it
    easy_drag=False,            # see below: NEVER use easy_drag
    transparent=True,           # lets the page paint rounded corners
)
```

- `easy_drag=True` makes the whole window a drag surface - which fights
  text selection, sliders, canvas interactions, and embedded terminals.
  Always `False`; implement an explicit drag region instead (§3).
- `transparent=True` makes the window surface itself transparent so the
  page can paint a rounded rectangle and let the corners show through.
  Needs a compositor (universal on modern desktops). The page must then
  paint its own background on a wrapper element - `html`/`body` stay
  transparent:

```css
html.frameless, html.frameless body { background: transparent; }
body.frameless #app { border-radius: 10px; background: var(--bg-0); }
body.frameless.maximized #app { border-radius: 0; }   /* flush when maxed */
```

- Ship an escape hatch (`--system-frame` flag → `frameless=False`) for
  broken environments and for debugging: report `frameless` to the
  frontend in an app-state call so the page only mounts its custom
  chrome when it should.
- A frameless window loses the 1px border compositors draw. Paint one
  yourself in a fixed overlay so the window edge reads against same-color
  backgrounds:

```css
#frame-border { position: fixed; inset: 0; pointer-events: none;
  border: 1px solid var(--bd-0); border-radius: 10px; }
```

## 2. Reaching the Qt window (the one helper everything uses)

All Qt calls MUST run on the Qt main thread - bridge calls arrive on
worker threads. One helper marshals safely and never raises:

```python
def _qt_window(self, fn):
    """Run fn(BrowserView) on the Qt main thread. Never raises."""
    from qtpy.QtCore import QTimer
    from qtpy.QtWidgets import QApplication
    from webview.platforms.qt import BrowserView

    def t():
        try:
            bv = BrowserView.instances.get(self._window.uid)
            if bv is not None:
                fn(bv)
        except Exception:
            pass
    QTimer.singleShot(0, QApplication.instance(), t)
```

`BrowserView.instances[window.uid]` is the actual `QMainWindow`
subclass; `bv.windowHandle()` is the `QWindow` that owns the native
move/resize primitives.

## 3. Dragging: the page-drawn titlebar

Backend - one bridge method, one line of substance:

```python
def win_drag(self):
    """Titlebar drag → compositor-native window move (Wayland + X11)."""
    self._qt_window(lambda bv: bv.windowHandle().startSystemMove())
    return {"ok": True}
```

Frontend - the top bar IS the titlebar. Two subtleties matter:

1. **Only "dead" areas drag.** Filter out anything interactive so
   buttons keep their clicks and inputs keep their focus:

```js
const isChrome = (e) =>
  !e.target.closest("button, select, input, a, textarea, .tab");
```

2. **A movement threshold, or double-click dies.** Calling
   `startSystemMove` on bare mousedown hands the pointer to the
   compositor immediately - which EATS the second click of a
   double-click. Start the system move only after ~4px of real motion:

```js
function wireDragRegion(region, isChrome) {
  let down = null;
  region.addEventListener("mousedown", (e) => {
    if (e.button === 0 && isChrome(e)) down = { x: e.clientX, y: e.clientY };
  });
  region.addEventListener("mousemove", (e) => {
    if (!down) return;
    if (Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 4) {
      down = null;
      Api.call("win_drag");        // gesture goes native from here
    }
  });
  window.addEventListener("mouseup", () => { down = null; });
  region.addEventListener("dblclick", (e) => {
    if (isChrome(e)) Api.call("win_toggle_max");   // dbl-click maximizes
  });
}
```

Also set `user-select: none` on the bar (drag attempts must not smear a
text selection), and remember that EVERY full-window surface needs its
own drag strip - a launch/picker screen shown before the main app
mounts still has to be draggable and closable.

## 4. Resizing: edge zones

Backend - map edge names onto Qt edge flags and go native:

```python
def win_resize(self, edges):
    """Edge-zone drag → compositor-native resize. edges: 'top',
    'bottom,right', ..."""
    def do(bv):
        from qtpy.QtCore import Qt
        m = {"top": Qt.Edge.TopEdge, "bottom": Qt.Edge.BottomEdge,
             "left": Qt.Edge.LeftEdge, "right": Qt.Edge.RightEdge}
        flags = None
        for part in str(edges or "").split(","):
            e = m.get(part.strip())
            if e is not None:
                flags = e if flags is None else (flags | e)
        if flags is not None:
            bv.windowHandle().startSystemResize(flags)
    self._qt_window(do)
    return {"ok": True}
```

Frontend - eight invisible fixed-position strips (4 edges + 4 corners),
each carrying its edge list, each just forwarding mousedown:

```html
<div id="resize-zones">
  <div class="rs rs-t"  data-edges="top"></div>
  <div class="rs rs-br" data-edges="bottom,right"></div>
  <!-- ... all 8 ... -->
</div>
```

```js
for (const z of document.querySelectorAll("#resize-zones .rs")) {
  z.addEventListener("mousedown", (e) => {
    if (e.button === 0 && !st.maximized) {   // no resizing while maxed
      e.preventDefault();
      Api.call("win_resize", z.dataset.edges);
    }
  });
}
```

CSS: ~6px wide strips (corners ~12px squares), `position: fixed`, a
z-index above the app, and the right `cursor` (`ns-resize`,
`nwse-resize`, …) per zone. The system minimum size still applies -
`min_size` keeps working frameless.

## 5. Window buttons: minimize / maximize / close

```python
def win_minimize(self):
    self._qt_window(lambda bv: bv.showMinimized())
    return {"ok": True}

def win_toggle_max(self):
    def do(bv):
        if bv.isMaximized():
            bv.showNormal()
        else:
            bv.showMaximized()
        # tell the page - it swaps the max/restore glyph and drops the
        # rounded corners while maximized
        self._bus.push({"type": "winstate", "maximized": bv.isMaximized()})
    self._qt_window(do)
    return {"ok": True}
```

- The page listens for `winstate` and toggles a `maximized` body class
  + the button icon (▢ vs ⧉). Push the same event from any other path
  that changes the state (double-click, WM shortcuts if you observe
  them) so the UI never lies.
- **Close is policy, not geometry**: route the custom ✕ through the same
  code path as a native close request (confirm-quit gating, hide-to-tray
  when a tray exists, cleanup). Never `window.destroy()` directly from
  the button handler.
- Buttons live in the titlebar markup like any other button - the
  `isChrome` filter (§3) already exempts them from dragging.

## 6. Modals: block the app, keep the titlebar alive

A modal with a click-blocking backdrop must NOT take the window chrome
down with it: while a dialog is up the user can still expect to move
the window, minimize it, or close it. The whole trick is one line of
CSS - start the backdrop BELOW the titlebar:

```css
#modal-root:not(:empty) {
  position: fixed;
  left: 0; right: 0; bottom: 0;
  top: var(--topbar-h);          /* ← the titlebar stays clickable */
  z-index: 100;
  background: rgba(0, 0, 0, .5);
  display: flex; align-items: center; justify-content: center;
}
```

- The backdrop covers and dims everything under the bar - clicks on the
  page behind the dialog die on the overlay - but drag, double-click
  maximize, and the min/max/close buttons keep working because the bar
  is simply not covered.
- Keep the resize zones (§4) at a HIGHER z-index than the backdrop if
  you want edge-resizing to survive modals too.
- `:not(:empty)` makes the backdrop exist only while a dialog is
  mounted - no pointer-events juggling, no state class to forget.

## 7. Gotchas checklist

- Qt calls off the main thread crash or silently fail - everything goes
  through the `_qt_window` helper. No exceptions.
- `easy_drag` looks tempting and is always wrong (breaks selection,
  terminals, sliders).
- No movement threshold → double-click maximize never fires (§3).
- `transparent=True` without the page painting its own background = an
  invisible window. Pair them, and drop the corner radius while
  maximized.
- Frameless kills the WM border - paint the 1px frame yourself (§1).
- Secondary surfaces (pickers, onboarding screens) each need their own
  drag strip and window controls.
- Test on Wayland AND X11: native move/resize is exactly what makes
  both work; anything geometry-based only ever worked on X11.
- Keep the `--system-frame` escape hatch wired end to end.
