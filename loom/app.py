"""Loom - the pywebview shell and the entire JS bridge surface.

Conventions (inherited from the pywebview knowledge base / the loom
incubator in CodeTree):
  * the frontend loads via a real ``file://`` URI - no web listener of any
    kind for the UI,
  * Ctrl/Cmd+Shift+I opens fully-local Chromium DevTools (a sibling Qt
    window via setDevToolsPage - never the appspot remote-debug redirect);
    ``--debug`` auto-opens them on load,
  * every JsApi method returns {"ok": True, ...} or {"ok": False, "error"},
  * long-running work (provider probes, chat streaming) runs on daemon
    worker threads and reports through Bus.push → evaluate_js - NEVER on a
    pywebview bridge thread (those are non-daemon and would block
    interpreter shutdown),
  * Loom does NOT launch inference - providers (llama-server / vendors) are
    other people's processes reached over HTTP, possibly through an ssh
    tunnel (key auth only).
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import webview

from loom import (apiserver, chat, chats, chatterm, compose, configedit,
                  containers, envs, libconfig, library, mcp, providers,
                  search, store, terminals)
from loom.compose import OUTPUT as FRONTEND_INDEX

DEFAULT_DEVTOOLS_PANEL = "console"


def harden_webengine(window):
    """pywebview's qt backend answers feature-permission requests with a raw
    int where PySide6 needs the enum - the JS promise hangs forever. Grant
    clipboard up front, then deny everything else with the proper enum."""
    from qtpy.QtCore import QTimer
    from qtpy.QtWidgets import QApplication
    from webview.platforms.qt import BrowserView

    def apply():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        try:
            from qtpy.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
            page = bv.webview.page()
            s = page.settings()
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanPaste, True)
            try:
                page.featurePermissionRequested.disconnect(page.onFeaturePermissionRequested)
            except Exception:
                pass

            def answer(origin, feature, _page=page):
                try:
                    _page.setFeaturePermission(
                        origin, feature,
                        QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
                except Exception:
                    pass

            page.featurePermissionRequested.connect(answer)
            bv._lm_perm_handler = answer
        except Exception:
            pass

    QTimer.singleShot(0, QApplication.instance(), apply)


def open_local_devtools(window, default_panel: str = DEFAULT_DEVTOOLS_PANEL):
    """Ctrl+Shift+I - Chromium's bundled DevTools in a sibling Qt window."""
    from qtpy.QtCore import QTimer
    from qtpy.QtWebEngineWidgets import QWebEngineView
    from qtpy.QtWidgets import QApplication
    from webview.platforms.qt import BrowserView

    def attach():
        bv = BrowserView.instances.get(window.uid)
        if bv is None:
            return
        existing = getattr(bv, "_devtools_view", None)
        if existing is not None:
            try:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                bv._devtools_view = None
        view = QWebEngineView()
        from qtpy.QtCore import Qt as _Qt
        view.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
        view.destroyed.connect(lambda *_: setattr(bv, "_devtools_view", None))
        view.setWindowTitle(f"DevTools - {window.title}")
        view.resize(1100, 750)
        bv.webview.page().setDevToolsPage(view.page())

        def pin_panel(ok):
            if not ok:
                return
            view.page().runJavaScript(
                f"""
                (function() {{
                    try {{
                        if (localStorage.getItem('panel-selectedTab') !== '{default_panel}') {{
                            localStorage.setItem('panel-selectedTab', '{default_panel}');
                            location.reload();
                        }}
                    }} catch (e) {{}}
                }})();
                """
            )
            try:
                view.page().loadFinished.disconnect(pin_panel)
            except Exception:
                pass

        view.page().loadFinished.connect(pin_panel)
        view.show()
        bv._devtools_view = view

    QTimer.singleShot(0, QApplication.instance(), attach)


# --------------------------------------------------------------------------
# icons - drawn at runtime; the weave glyph matches the in-app logo

def _base_pixmap(size=64):
    """Rounded zinc tile with a woven warp/weft glyph (white on near-black
    - matches the app theme and reads on any tray background)."""
    from qtpy.QtCore import Qt
    from qtpy.QtGui import QColor, QPainter, QPen, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#18181b"))
    r = size * 0.22
    p.drawRoundedRect(size * 0.03, size * 0.03, size * 0.94, size * 0.94, r, r)
    strand = QColor("#fafafa")
    w = size * 0.09
    pen = QPen(strand, w)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    # weave: two warps, two wefts, over-under (gaps fake the interlacing)
    x1, x2 = size * 0.38, size * 0.62
    y1, y2 = size * 0.38, size * 0.62
    lo, hi = size * 0.20, size * 0.80
    g = w * 0.9
    p.drawLine(int(x1), int(lo), int(x1), int(y2 - g))
    p.drawLine(int(x1), int(y2 + g), int(x1), int(hi))
    p.drawLine(int(x2), int(lo), int(x2), int(y1 - g))
    p.drawLine(int(x2), int(y1 + g), int(x2), int(hi))
    p.drawLine(int(lo), int(y1), int(hi), int(y1))
    p.drawLine(int(lo), int(y2), int(x1 - g), int(y2))
    p.drawLine(int(x1 + g), int(y2), int(hi), int(y2))
    p.end()
    return pm


def make_app_icon():
    from qtpy.QtGui import QIcon
    return QIcon(_base_pixmap(64))


# tray / close-to-tray state, shared between the closing handler (worker
# thread) and the tray setup (Qt main thread)
_tray_state = {"tray": None, "available": False, "notified": False}


def _set_tray_tooltip(text: str) -> None:
    """Tray tooltip = 'Loom - <library folder>' - how the user tells
    multiple Loom tray icons apart. Marshalled to the Qt main thread;
    silently a no-op when there is no tray (headless)."""
    tray = _tray_state.get("tray")
    if tray is None:
        return
    try:
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        QTimer.singleShot(0, QApplication.instance(),
                          lambda: tray.setToolTip(str(text)))
    except Exception:
        pass


_TRAY_MSG_MS = 10000


def _tray_notify(title: str, text: str) -> None:
    """Tray balloon, KDE-proof, marshalled to the Qt main thread (the
    closing handler runs on a worker thread - raw Qt calls there are UB).
    On KDE the tray is a StatusNotifierItem, and Qt's showMessage sets the
    item's ATTENTION icon to the message icon (default: the blue
    dialog-information "i") and flips the status to NeedsAttention -
    Plasma then renders that instead of our icon, and is known to leave it
    stuck. Passing OUR icon makes the attention icon indistinguishable
    from the real one, and re-asserting setIcon after the timeout nudges
    Plasma back to normal in either case."""
    tray = _tray_state.get("tray")
    if tray is None:
        return
    try:
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication

        def do():
            try:
                tray.showMessage(str(title), str(text), make_app_icon(),
                                 _TRAY_MSG_MS)
            except Exception:
                return
            QTimer.singleShot(_TRAY_MSG_MS + 1000, tray,
                              lambda: tray.setIcon(make_app_icon()))
        QTimer.singleShot(0, QApplication.instance(), do)
    except Exception:
        pass


def _qt_focus(window):
    """Raise + activate + un-hide the window. Runs inside Qt's loop and
    must never raise (an exception there corrupts pywebview's eventFilter)."""
    try:
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        from webview.platforms.qt import BrowserView

        def do():
            try:
                bv = BrowserView.instances.get(window.uid)
                if bv is None:
                    return
                if bv.isMinimized():
                    bv.showNormal()
                else:
                    bv.show()
                bv.raise_()
                bv.activateWindow()
            except Exception:
                pass
        QTimer.singleShot(0, QApplication.instance(), do)
    except Exception:
        try:
            window.show()
        except Exception:
            pass


def attempt_quit(bus: "Bus", window, closing_main: bool = False,
                 force: bool = False) -> bool:
    """The ONE quit gate every real-quit path takes (tray Quit, no-tray
    window close, signals). Blocked by streaming chats or live terminals:
    the window is brought up WITH the confirmation dialog showing.
    closing_main: called from the window's own closing handler - pywebview
    finishes the close itself, we must not destroy() recursively."""
    blockers = len(chat.running_chats()) + terminals.live_count()
    if not force and blockers:
        _qt_focus(window)   # the decision surfaces in the main window
        bus.push({"type": "confirm_quit",
                  "chats": len(chat.running_chats()),
                  "terminals": terminals.live_count()})
        return False
    _quit_state["quitting"] = True
    save_geometry()
    close_child_windows(bus)   # children never outlive the main window
    tray = _tray_state.get("tray")
    if tray is not None:
        try:
            tray.hide()
        except Exception:
            pass
    if not closing_main:
        try:
            window.destroy()
        except Exception:
            pass
    return True


def setup_tray(window, api: "JsApi", bus: "Bus"):
    """System tray on the Qt main thread. No tray available (headless,
    some desktops) → close-to-tray is disabled so the user is never
    trapped with no way back to the window. The menu is rebuilt on
    aboutToShow (Qt main thread - safe to read live state)."""
    from qtpy.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    app = QApplication.instance()
    if app is None:
        return
    try:
        # the StatusNotifierItem Id on KDE comes from desktopFileName and
        # falls back to the binary name - "python3" for us, which every
        # Loom instance would share and plasmashell caches tray state by.
        # Claim a proper identity before the tray registers.
        from qtpy.QtGui import QGuiApplication
        QGuiApplication.setDesktopFileName("loom")
    except Exception:
        pass
    icon = make_app_icon()
    app.setWindowIcon(icon)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        _tray_state["available"] = False
        return

    tray = QSystemTrayIcon(icon)
    # the tooltip names the open library - with several Looms on several
    # libraries, this is how the user tells the tray icons apart
    tray.setToolTip("Loom - " + api._root.name if api._root else "Loom")
    menu = QMenu()

    def do_show():
        _qt_focus(window)

    def rebuild():
        menu.clear()
        menu.addAction("Show Loom").triggered.connect(do_show)
        menu.addSeparator()
        # providers at a glance: name + reachability from the last probe
        provs = providers.snapshot()
        for p in provs:
            mark = {"ok": "●", "error": "▲"}.get(p.get("state"), "○")
            act = menu.addAction(f"{mark} {p.get('name')}")
            act.setEnabled(False)
        if not provs and api._root is None:
            menu.addAction("(no library open)").setEnabled(False)
        menu.addSeparator()
        # the OpenAI-compatible API - toggleable from the tray, and always
        # OFF at launch (the on/off state is never persisted)
        ast = apiserver.status()
        act = menu.addAction(
            f"API server - on ({ast['interface']}:{ast['port']})"
            if ast["running"] else "API server - off")
        act.setCheckable(True)
        act.setChecked(ast["running"])
        act.setEnabled(api._root is not None)

        def toggle_api(_=False, want=not ast["running"]):
            def work():
                got = api.api_server_toggle(want)
                if not got.get("ok"):
                    bus.push({"type": "toast", "level": "err",
                              "msg": got.get("error") or "API toggle failed"})
            threading.Thread(target=work, daemon=True,
                             name="tray-api").start()
        act.triggered.connect(toggle_api)
        menu.addSeparator()
        # quit runs on a WORKER thread: the gate may surface a confirm in
        # the window - never block the Qt main loop
        menu.addAction("Quit Loom").triggered.connect(
            lambda _=False: threading.Thread(
                target=lambda: attempt_quit(bus, window),
                daemon=True, name="quit-gate").start())

    menu.aboutToShow.connect(rebuild)
    rebuild()
    tray.activated.connect(
        lambda reason: do_show() if reason == QSystemTrayIcon.Trigger else None)
    tray.setContextMenu(menu)
    tray.show()
    _tray_state["tray"] = tray
    _tray_state["available"] = True


class Bus:
    """Pushes events into the page(s): LM_onEvent({...}). One dedicated
    daemon sender thread - evaluate_js blocks until Qt's main loop runs
    it, so sending from the main thread would deadlock and a bridge-
    thread send could hang process exit.

    Besides the MAIN window, child windows (popped-out diagnostics) can
    register for the same event stream - every push broadcasts to all of
    them; each page filters for what it cares about."""

    def __init__(self):
        self._window = None
        self._extra: list = []      # registered child windows
        self._backlog: list[dict] = []
        self._lock = threading.Lock()
        import queue as _queue
        self._sendq: "_queue.SimpleQueue" = _queue.SimpleQueue()
        threading.Thread(target=self._sender, daemon=True, name="bus-sender").start()

    def _sender(self) -> None:
        while True:
            win, payload = self._sendq.get()
            try:
                win.evaluate_js(payload)
            except Exception:
                pass

    def set_window(self, window):
        with self._lock:
            self._window = window
            backlog, self._backlog = self._backlog, []
        for ev in backlog:
            self.push(ev)

    def add_window(self, window) -> None:
        with self._lock:
            if window not in self._extra:
                self._extra.append(window)

    def remove_window(self, window) -> None:
        with self._lock:
            if window in self._extra:
                self._extra.remove(window)

    def push(self, event: dict) -> None:
        with self._lock:
            win = self._window
            extra = list(self._extra)
            if win is None:
                self._backlog.append(event)
                return
        payload = f"window.LM_onEvent && LM_onEvent({json.dumps(event)})"
        self._sendq.put((win, payload))
        for w in extra:
            self._sendq.put((w, payload))


def _api_call(fn):
    """Exceptions become {ok:false,error} - the bridge never dies."""
    def inner(*a, **kw):
        try:
            out = fn(*a, **kw)
            if isinstance(out, dict) and "ok" in out:
                return out
            return {"ok": True, "data": out}
        except (providers.ProviderError, library.LibraryError,
                libconfig.ConfigError, chats.ChatError,
                containers.ContainerError, terminals.TermError,
                mcp.McpError, apiserver.ApiError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return inner


_quit_state = {"quitting": False}


# --------------------------------------------------------------------------
# main-window geometry: tracked live, saved on every quit path, restored
# at launch - the app reopens the size and place it was closed in.

_geom: dict = {}


def sane_geometry(geo: dict) -> dict:
    """Validate saved geometry before trusting it: sizes must be
    plausible, positions must not strand the window in the void (a
    monitor that no longer exists gets the default placement)."""
    out = {}
    try:
        w, h = int(geo.get("width")), int(geo.get("height"))
        if 400 <= w <= 20000 and 300 <= h <= 20000:
            out["width"], out["height"] = w, h
    except (TypeError, ValueError):
        pass
    try:
        x, y = int(geo.get("x")), int(geo.get("y"))
        if -20000 <= x <= 20000 and -20000 <= y <= 20000:
            out["x"], out["y"] = x, y
    except (TypeError, ValueError):
        pass
    if geo.get("maximized"):
        out["maximized"] = True
    return out


def _watch_geometry(window, bus: "Bus") -> None:
    """Mirror the window's live geometry into _geom. pywebview's qt
    backend fires these from the Qt main thread, so reading the
    maximized flag directly is safe; a maximized window's oversize
    dimensions are NOT recorded - restoring should re-maximize on top
    of the last normal size.

    The maximized flag is also PUSHED to the page whenever it changes:
    the compositor can (un)maximize without the app's own toggle -
    dragging a maximized window unmaximizes it in KWin, shortcuts and
    edge-snaps exist too - and a page stuck on body.maximized loses its
    rounded corners and hides every resize zone."""
    def is_max():
        try:
            from webview.platforms.qt import BrowserView
            bv = BrowserView.instances.get(window.uid)
            return bool(bv is not None and bv.isMaximized())
        except Exception:
            return False

    pushed = {"max": None}

    def sync_max(m):
        _geom["maximized"] = m
        if pushed["max"] != m:
            pushed["max"] = m
            bus.push({"type": "winstate", "maximized": m})

    def on_resized(w, h):
        m = is_max()
        sync_max(m)
        if not m:
            _geom["width"], _geom["height"] = int(w), int(h)

    def on_moved(x, y):
        if not is_max():
            _geom["x"], _geom["y"] = int(x), int(y)
    window.events.resized += on_resized
    window.events.moved += on_moved
    # the flag also rides the dedicated events (resize can fire with the
    # maximized dimensions BEFORE the Qt state flips - these settle it)
    window.events.maximized += lambda *a: sync_max(True)
    window.events.restored += lambda *a: sync_max(is_max())


def save_geometry() -> None:
    """Best-effort persist - called from every path that ends or hides
    the window."""
    if _geom.get("width") or _geom.get("maximized"):
        try:
            store.set_window_geometry(dict(_geom))
        except Exception:
            pass


def _qt_maximize(window) -> None:
    try:
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        from webview.platforms.qt import BrowserView

        def do():
            try:
                bv = BrowserView.instances.get(window.uid)
                if bv is not None:
                    bv.showMaximized()
            except Exception:
                pass
        QTimer.singleShot(0, QApplication.instance(), do)
    except Exception:
        pass


# --------------------------------------------------------------------------
# child windows (popped-out diagnostics, and whatever comes next). The
# rule: children never outlive the MAIN window - closing it (to the tray
# or for real) and leaving the library both take every child down.

_children_lock = threading.Lock()
_child_windows: dict[str, object] = {}   # key (e.g. "diag:<chatId>") -> window


def register_child_window(key: str, win, bus: "Bus") -> None:
    with _children_lock:
        _child_windows[key] = win
    bus.add_window(win)

    def on_closed():
        with _children_lock:
            if _child_windows.get(key) is win:
                del _child_windows[key]
        bus.remove_window(win)
    win.events.closed += on_closed


def pop_child_window(key: str, bus: "Bus"):
    """Unregister and return the window (or None) - the caller destroys."""
    with _children_lock:
        win = _child_windows.pop(key, None)
    if win is not None:
        bus.remove_window(win)
    return win


def child_window(key: str):
    with _children_lock:
        return _child_windows.get(key)


def close_child_windows(bus: "Bus") -> None:
    with _children_lock:
        wins = list(_child_windows.values())
        _child_windows.clear()
    for w in wins:
        bus.remove_window(w)
        try:
            w.destroy()
        except Exception:
            pass


def _force_exit(code: int = 0, grace: float = 2.0) -> None:
    """Terminate the PROCESS. os._exit ends the interpreter
    unconditionally - non-daemon pywebview bridge threads must never hold
    a corpse alive. Providers are other people's processes; nothing of
    theirs depends on our exit."""
    save_geometry()   # even a force-quit reopens where it was
    os._exit(code)


def make_signal_handler(notify, force, window: float = 3.0,
                        _clock=time.monotonic):
    """SIGINT/SIGTERM policy: one isolated signal → notify (surface the
    quit confirmation; some other program or the OS poking us must never
    kill work). A SECOND signal within `window` seconds → force: the user
    is spamming Ctrl+C and the app OBEYS. A signal long after the last one
    counts as isolated again."""
    times: list[float] = []

    def handler(signum=None, frame=None):
        now = _clock()
        times[:] = [t for t in times if now - t < window]
        times.append(now)
        if len(times) >= 2:
            force()
            return
        notify()
    return handler


class JsApi:
    """The entire backend surface for the page."""

    def __init__(self, bus: Bus):
        self._bus = bus
        self._window = None
        self._root: Path | None = None   # the open library

    def set_window(self, window):
        self._window = window

    def _toast(self, level: str, msg: str):
        self._bus.push({"type": "toast", "level": level,
                        "msg": providers.tilde(msg)})

    def _need_root(self) -> Path:
        if self._root is None:
            raise library.LibraryError("no library is open")
        return self._root

    # ---------------- app basics ----------------
    def ping(self):
        return {"ok": True, "pong": True}

    # ---------------- frameless window controls ----------------
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

    def win_minimize(self):
        self._qt_window(lambda bv: bv.showMinimized())
        return {"ok": True}

    def win_toggle_max(self):
        def do(bv):
            if bv.isMaximized():
                bv.showNormal()
            else:
                bv.showMaximized()
            self._bus.push({"type": "winstate", "maximized": bv.isMaximized()})
        self._qt_window(do)
        return {"ok": True}

    def win_close(self):
        """The custom titlebar's ✕ - same semantics as a native close:
        tray-hide when available, else the gated quit."""
        def work():
            if on_window_closing(self._window, self._bus):
                _quit_state["quitting"] = True
                try:
                    self._window.destroy()
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True, name="win-close").start()
        return {"ok": True}

    def win_drag(self):
        """Titlebar drag → compositor-native window move (Wayland + X11)."""
        self._qt_window(lambda bv: bv.windowHandle().startSystemMove())
        return {"ok": True}

    def win_resize(self, edges):
        """Edge-zone drag → compositor-native resize. edges: 'top', 'left',
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

    def open_devtools(self):
        if self._window is not None:
            open_local_devtools(self._window)
        return {"ok": True}

    def app_state(self):
        def do():
            st = store.load_state()
            out = {"theme": st.get("theme") or "dark",
                   "recents": store.visible_recents(),
                   "frameless": bool(getattr(self, "_frameless", False)),
                   # the page (re)builds body.maximized from this - a
                   # stale class squares the corners and kills resizing
                   "maximized": bool(_geom.get("maximized")),
                   "library": str(self._root) if self._root else None}
            if self._root is not None:
                out["config"] = self._config_or_error()
            return out
        return _api_call(do)()

    def set_theme(self, theme):
        def do():
            store.mutate_state(lambda st: st.__setitem__(
                "theme", "light" if theme == "light" else "dark"))
            return {}
        return _api_call(do)()

    # ---------------- library picker ----------------
    def recents_get(self):
        return _api_call(lambda: {"recents": store.visible_recents()})()

    def recents_clear(self):
        def do():
            store.clear_recents()
            return {"recents": store.visible_recents()}
        return _api_call(do)()

    def recent_remove(self, path, omit=False):
        def do():
            store.remove_recent(str(path), omit=bool(omit))
            return {"recents": store.visible_recents()}
        return _api_call(do)()

    def open_external(self, url):
        """Hand a URI to the OS default handler. Only ever called after
        the user confirmed in the UI; the scheme is checked again here and
        the dangerous ones are refused outright."""
        def do():
            import re as _re
            import webbrowser
            u = str(url or "").strip()
            if not _re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", u):
                raise chats.ChatError("not an absolute URI")
            scheme = u.split(":", 1)[0].lower()
            if scheme in ("javascript", "data", "file", "vbscript", "blob"):
                raise chats.ChatError(f"refusing to open a {scheme}: link")
            webbrowser.open(u)
            return {}
        return _api_call(do)()

    def pick_folder(self):
        def do():
            if self._window is None:
                raise library.LibraryError("window not ready")
            got = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            if not got:
                return {"path": None}
            return {"path": str(got[0])}
        return _api_call(do)()

    def pick_images(self):
        def do():
            if self._window is None:
                raise library.LibraryError("window not ready")
            got = self._window.create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=True,
                file_types=("Images (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp)",))
            return {"paths": [str(p) for p in (got or [])]}
        return _api_call(do)()

    def templates_list(self):
        return _api_call(lambda: {"templates": library.list_templates()})()

    def library_create(self, path, template="starter"):
        def do():
            root = library.create_library(str(path), str(template or "starter"))
            return self._open(root)
        return _api_call(do)()

    def library_open(self, path):
        def do():
            root = library.open_library(str(path))
            return self._open(root)
        return _api_call(do)()

    def _open(self, root: Path) -> dict:
        # one Loom per library: claim it (raises if another live instance
        # holds it), and drop any previous library's claim
        library.acquire_lock(root)
        if self._root is not None and self._root != root:
            library.release_lock(self._root)
        self._root = root
        _set_tray_tooltip("Loom - " + root.name)
        store.touch_recent(str(root))
        cfg = self._config_or_error()
        # probe this library's providers in the background - the model
        # lists and context sizes land via the providers event stream
        if isinstance(cfg, dict) and not cfg.get("error"):
            threading.Thread(target=lambda: providers.refresh(cfg),
                             daemon=True, name="prov-refresh").start()
        # MCP: servers running when the library last closed come back up
        mcp.set_library(str(root), self._bus.push)
        if isinstance(cfg, dict) and not cfg.get("error"):
            mcps = cfg.get("mcpServers") or []
            threading.Thread(target=lambda: mcp.autostart(mcps),
                             daemon=True, name="mcp-autostart").start()
        chats.purge_empty_archived(root)   # empty chats are never worth keeping
        open_chats = [c for c in chats.list_chats(root) if not c["archived"]]
        sess = store.session_get(str(root))
        return {"library": str(root), "config": cfg, "openChats": open_chats,
                "session": sess,
                # no saved session = the first visit - land on the docs
                "firstOpen": sess is None}

    def _config_or_error(self):
        try:
            return libconfig.load(self._need_root())
        except libconfig.ConfigError as e:
            return {"error": str(e)}

    # ------------- config editing (the Config tab + section forms) -------------
    def _config_text_update(self, mutate):
        """The shared spine of every structured config writer: read
        loom.yaml, run mutate(text) → new text, validate the WHOLE result,
        write atomically, push the config event. Returns the parsed cfg."""
        root = self._need_root()
        p = library.config_path(root)
        if p is None:
            raise libconfig.ConfigError("the library has no loom.yaml")
        try:
            new_text = mutate(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise libconfig.ConfigError(str(e))
        try:
            cfg = libconfig.parse_text(new_text, p.name)
        except libconfig.ConfigError as e:
            raise libconfig.ConfigError(f"{e} - nothing was written")
        library.write_file(root, p.name, new_text)
        self._bus.push({"type": "config", "config": cfg})
        return cfg

    def config_read(self):
        """loom.yaml's raw text + mtime for the Config tab's editor."""
        def do():
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            return library.read_file(root, p.name)
        return _api_call(do)()

    def config_write(self, text, base_mtime=None):
        """Save the Config tab's buffer: the WHOLE candidate must validate
        or nothing is written. `base_mtime` (from config_read) catches the
        file changing under a dirty buffer. On success the runtime
        converges - providers re-probe, a running API server rebinds if
        its address changed, MCP servers dropped from config stop."""
        def do():
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            if base_mtime is not None:
                if int(base_mtime) != int(p.stat().st_mtime * 1000):
                    raise libconfig.ConfigError(
                        f"{p.name} changed on disk since the editor loaded "
                        "it - nothing was written. Reload the editor, then "
                        "re-apply your changes.")
            body = str(text or "")
            if body and not body.endswith("\n"):
                body += "\n"
            try:
                cfg = libconfig.parse_text(body, p.name)
            except libconfig.ConfigError as e:
                raise libconfig.ConfigError(f"{e} - nothing was written")
            out = library.write_file(root, p.name, body)
            self._bus.push({"type": "config", "config": cfg})
            self._reapply_runtime(cfg)
            return {"configFile": p.name, "mtime": out["mtime"]}
        return _api_call(do)()

    def _reapply_runtime(self, cfg):
        """Converge live state after a full-config save."""
        threading.Thread(target=lambda: providers.refresh(cfg),
                         daemon=True, name="prov-refresh-cfg").start()
        if apiserver.is_running():
            api = cfg.get("api") or dict(libconfig.DEFAULT_API)
            got = apiserver.status()
            if (got.get("interface"), got.get("port")) != \
                    (api.get("interface"), api.get("port")):
                apiserver.stop()
                try:
                    apiserver.start(api["interface"], api["port"],
                                    self._api_providers, self._api_key())
                except apiserver.ApiError as e:
                    self._toast("err", f"API server: {e}")
                self._bus.push({"type": "apisrv", **apiserver.status(),
                                "hasKey": bool(self._api_key())})
        names = {s["name"] for s in cfg.get("mcpServers") or []}
        stopped = False
        for n in list(mcp.running() or {}):
            if n not in names:
                mcp.stop_server(n)
                stopped = True
        if stopped:
            self._bus.push({"type": "mcp", "kind": "config"})

    def chat_config_set(self, chat_cfg):
        """Persist the Chat-defaults dialog into loom.yaml's `chat:` block
        (values matching the defaults are omitted; an all-default block
        is removed entirely)."""
        def do():
            c = chat_cfg if isinstance(chat_cfg, dict) else {}
            self._config_text_update(
                lambda text: configedit.replace_section(
                    text, ("chat",), configedit.chat_lines(c)))
            return {}
        return _api_call(do)()

    def permission_modes_set(self, modes):
        """Persist the permission-modes dialog: full effective
        {mode: {tool: level}} maps in, minimal per-mode diffs out. The
        legacy `permissions:` section is removed in the same write - the
        diff already carries whatever it contributed."""
        def do():
            m = modes if isinstance(modes, dict) else {}

            def mutate(text):
                text = configedit.replace_section(text, ("permissions",), [])
                return configedit.replace_section(
                    text, ("permission-modes", "permission_modes"),
                    configedit.permission_mode_lines(m))
            self._config_text_update(mutate)
            return {}
        return _api_call(do)()

    def containers_config_set(self, containers_cfg):
        """Persist the Containers dialog into loom.yaml's `containers:`
        block (defaults omitted)."""
        def do():
            c = containers_cfg if isinstance(containers_cfg, dict) else {}
            self._config_text_update(
                lambda text: configedit.replace_section(
                    text, ("containers",), configedit.container_lines(c)))
            return {}
        return _api_call(do)()

    def session_save(self, session):
        """The frontend pushes its UI session (tabs, active tab, open file,
        tree expansion, panel width) whenever it changes - reopening the
        library restores it."""
        def do():
            root = self._need_root()
            store.session_set(str(root),
                              session if isinstance(session, dict) else {})
            return {}
        return _api_call(do)()

    # -------- alerts: kept per library until the user clears them --------
    def alerts_get(self):
        return _api_call(lambda: {
            "alerts": store.alerts_get(str(self._need_root()))})()

    def alert_add(self, item):
        def do():
            store.alerts_add(str(self._need_root()),
                             item if isinstance(item, dict) else {})
            return {}
        return _api_call(do)()

    def alerts_clear(self):
        def do():
            store.alerts_clear(str(self._need_root()))
            return {}
        return _api_call(do)()

    # ---------------- library tab (files) ----------------
    def lib_tree(self, show_hidden=False):
        return _api_call(lambda: {
            "tree": library.tree(self._need_root(), bool(show_hidden)),
            "configFile": (library.config_path(self._need_root())
                           or Path("loom.yaml")).name})()

    def lib_read(self, rel):
        return _api_call(lambda: library.read_file(self._need_root(), str(rel)))()

    def lib_write(self, rel, text):
        def do():
            out = library.write_file(self._need_root(), str(rel), str(text))
            name = (library.config_path(self._need_root()) or Path()).name
            if str(rel) == name:
                # config edited: re-parse and tell the page (Servers tab)
                self._bus.push({"type": "config", "config": self._config_or_error()})
            return out
        return _api_call(do)()

    def lib_create(self, rel, directory=False):
        return _api_call(lambda: library.create_entry(
            self._need_root(), str(rel), bool(directory)))()

    def lib_rename(self, rel, new_rel):
        return _api_call(lambda: library.rename_entry(
            self._need_root(), str(rel), str(new_rel)))()

    def lib_delete(self, rel):
        return _api_call(lambda: library.delete_entry(self._need_root(), str(rel)))()

    def lib_search(self, query):
        return _api_call(lambda: {"results": search.search(
            self._need_root(), str(query or ""))})()

    # ---------------- providers ----------------
    # a provider's API key (its server was started with --api-key) lives
    # in the OS keyring, per library and provider name - never in the
    # shareable loom.yaml. providers.request() pulls it through the
    # resolver installed in main().
    def _provider_key_name(self, name: str) -> str:
        return f"loom-provider-key::{self._need_root()}::{name}"

    def _provider_key(self, name: str) -> str:
        try:
            return envs._kr_get(self._provider_key_name(str(name))) or ""
        except Exception:
            return ""   # no library, or no keyring backend: no key

    def providers_get(self):
        """Config + the live registry, merged per provider."""
        def do():
            cfg = self._config_or_error()
            states = {r.get("name"): r for r in providers.snapshot()}
            rows = []
            for p in (cfg.get("providers") or []) if not cfg.get("error") else []:
                st = states.get(p["name"]) or {}
                rows.append({**p,
                             "state": st.get("state") or "unknown",
                             "detail": st.get("detail") or "not probed yet",
                             "models": st.get("models") or [],
                             "hasKey": bool(self._provider_key(p["name"])),
                             "ts": st.get("ts")})
            return {"providers": rows, "config": cfg}
        return _api_call(do)()

    def provider_key_set(self, name, key):
        """Set (or, empty, clear) one provider's API key in the OS
        keyring, then re-probe it - the key applies to every request
        from now on."""
        def do():
            cfg = libconfig.load(self._need_root())
            rec = libconfig.provider_by_name(cfg, str(name or ""))
            if rec is None:
                raise providers.ProviderError(
                    f"no provider named {name!r} in loom.yaml")
            k = str(key or "").strip()
            try:
                if k:
                    envs._kr_set(self._provider_key_name(rec["name"]), k)
                else:
                    envs._kr_del(self._provider_key_name(rec["name"]))
            except Exception as e:
                raise providers.ProviderError(
                    f"cannot reach the OS keyring: {e}")
            threading.Thread(target=lambda: providers.probe(rec),
                             daemon=True, name="prov-key-probe").start()
            return {"provider": rec["name"], "hasKey": bool(k)}
        return _api_call(do)()

    def providers_refresh(self):
        """Re-probe every configured provider and push fresh states."""
        def do():
            cfg = self._config_or_error()
            if not cfg.get("error"):
                threading.Thread(target=lambda: providers.refresh(cfg),
                                 daemon=True, name="prov-refresh-ui").start()
            return {}
        return _api_call(do)()

    def provider_probe(self, name):
        """Probe ONE provider synchronously - the menu's refresh."""
        def do():
            cfg = libconfig.load(self._need_root())
            rec = libconfig.provider_by_name(cfg, str(name or ""))
            if rec is None:
                raise providers.ProviderError(
                    f"no provider named {name!r} in loom.yaml")
            return {"provider": providers.probe(rec)}
        return _api_call(do)()

    def provider_test(self, url, ssh="", vendor="llama-cpp", key=""):
        """The Add-provider dialog's Test button - an ad-hoc probe that
        never touches config, registry, or keyring. `key` rides along so
        a keyed API can be tested before anything is stored."""
        def do():
            rec = {"name": "(test)",
                   "vendor": str(vendor or "llama-cpp"),
                   "url": str(url or "").rstrip("/"),
                   "ssh": str(ssh or "").strip(),
                   "key": str(key or "").strip()}
            return {"result": providers.probe(rec, register=False)}
        return _api_call(do)()

    def provider_add(self, name, vendor, url, ssh="", key=""):
        """Append one provider entry to loom.yaml non-destructively,
        validating the result before writing. A given `key` lands in the
        OS keyring BEFORE the refresh probes, so an --api-key server
        answers its very first probe."""
        def do():
            import yaml as _yaml
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            k = str(key or "").strip()
            if k:
                try:
                    envs._kr_set(
                        self._provider_key_name(str(name or "").strip()), k)
                except Exception as e:
                    raise providers.ProviderError(
                        f"cannot reach the OS keyring: {e}")
            new_text = providers.inject_provider(
                p.read_text(encoding="utf-8"), str(name or "").strip(),
                str(vendor or "llama-cpp").strip(), str(url or "").strip(),
                str(ssh or "").strip())
            try:
                raw = _yaml.safe_load(new_text) or {}
                libconfig._providers(raw.get("providers"))
            except _yaml.YAMLError as e:
                raise libconfig.ConfigError(
                    f"the result would not parse - nothing was written: {e}")
            library.write_file(root, p.name, new_text)
            self._bus.push({"type": "config", "config": self._config_or_error()})
            cfg = self._config_or_error()
            if not cfg.get("error"):
                threading.Thread(target=lambda: providers.refresh(cfg),
                                 daemon=True, name="prov-refresh-add").start()
            return {"configFile": p.name}
        return _api_call(do)()

    def provider_update(self, name, new_name, vendor, url, ssh="", key=""):
        """Rewrite one provider's loom.yaml entry (the card's Edit dialog),
        validating the whole result before writing. A rename moves the
        provider's keyring key along; a non-empty `key` replaces the
        stored one."""
        def do():
            old = str(name or "").strip()
            new = str(new_name or "").strip() or old
            cfg = self._config_text_update(
                lambda text: configedit.replace_list_item(
                    text, ("providers",), old,
                    providers.entry_lines(
                        new, str(vendor or "llama-cpp").strip(),
                        str(url or "").strip(), str(ssh or "").strip())))
            k = str(key or "").strip()
            try:
                if k:
                    envs._kr_set(self._provider_key_name(new), k)
                    if new != old:
                        envs._kr_del(self._provider_key_name(old))
                elif new != old:
                    moved = envs._kr_get(self._provider_key_name(old)) or ""
                    if moved:
                        envs._kr_set(self._provider_key_name(new), moved)
                        envs._kr_del(self._provider_key_name(old))
            except Exception:
                pass   # the entry is saved; the keyring move is best-effort
            threading.Thread(target=lambda: providers.refresh(cfg),
                             daemon=True, name="prov-refresh-edit").start()
            return {"provider": new}
        return _api_call(do)()

    def provider_remove(self, name):
        """Delete one provider from loom.yaml (and its keyring key). The
        refresh prunes it from the live registry."""
        def do():
            n = str(name or "").strip()
            cfg = self._config_text_update(
                lambda text: configedit.remove_list_item(
                    text, ("providers",), n))
            try:
                envs._kr_del(self._provider_key_name(n))
            except Exception:
                pass
            threading.Thread(target=lambda: providers.refresh(cfg),
                             daemon=True, name="prov-refresh-del").start()
            return {"removed": n}
        return _api_call(do)()

    def provider_models(self, name):
        """The cached model list for one provider ([{id, ctx}]) - the
        model menu's data. Empty + never probed → probe now (blocking,
        but only the first open pays it)."""
        def do():
            cfg = libconfig.load(self._need_root())
            rec = libconfig.provider_by_name(cfg, str(name or ""))
            if rec is None:
                raise providers.ProviderError(
                    f"no provider named {name!r} in loom.yaml")
            got = providers.status_of(rec["name"])
            if not got:
                got = providers.probe(rec)
            return {"provider": rec["name"], "state": got.get("state"),
                    "detail": got.get("detail") or "",
                    "models": got.get("models") or []}
        return _api_call(do)()

    # ---------------- chats ----------------
    def chats_list(self):
        return _api_call(lambda: {"chats": chats.list_chats(self._need_root())})()

    @staticmethod
    def _clean_folders(folders):
        clean = []
        for f in folders if isinstance(folders, list) else []:
            p = str((f or {}).get("path") or "").strip()
            if not p:
                continue
            mode = "write" if (f or {}).get("mode") == "write" else "view"
            clean.append({"path": p, "mode": mode})
        return clean

    def chat_new(self, template=None):
        """New chat. `template` (from Ctrl+Shift+N on an open chat) carries the
        source chat's model, permission mode, and folder attachments - a
        clean context window with the same working setup."""
        def do():
            cfg = self._config_or_error()
            provider, model = "", ""
            if not cfg.get("error"):
                provider = (cfg.get("chat") or {}).get("provider") or ""
                model = (cfg.get("chat") or {}).get("model") or ""
            t = template if isinstance(template, dict) else {}
            if str(t.get("model") or "").strip() \
                    or str(t.get("provider") or "").strip():
                model = str(t.get("model") or "").strip()
                provider = str(t.get("provider") or "").strip()
            c = chats.new_chat(self._need_root(), model, provider)
            # loom.yaml's thought_truncation is the DEFAULT for new
            # chats - stamped here so later config edits leave existing
            # chats alone (the chat's own chip governs from now on)
            if not cfg.get("error"):
                c["thoughtTruncation"] = bool(
                    (cfg.get("chat") or {}).get("thought_truncation", True))
                chats.save_chat(self._need_root(), c)
            pm = str(t.get("permMode") or "").strip()
            if pm and (cfg.get("error") or pm in (cfg.get("permissionModes") or {})):
                c["permMode"] = pm
            folders = self._clean_folders(t.get("folders"))
            if folders:
                c["folders"] = folders
            net = containers.net_mode(t.get("network"))
            if net != "none":
                c["network"] = net
            if pm or folders or net != "none":
                chats.save_chat(self._need_root(), c)
            # the context chip must have real values from the first paint
            try:
                if not cfg.get("error"):
                    c["context"] = chat.context_breakdown(
                        self._need_root(), cfg, c)
            except Exception:
                pass
            c.setdefault("context", None)
            c["totalMessages"] = 0
            return {"chat": c}
        return _api_call(do)()

    def chat_fork(self, chat_id, upto_index):
        """Fork a chat at a message: a NEW chat whose history is the
        source truncated after `upto_index` (an ABSOLUTE index - the
        frontend only holds a window). Setup carries over; if tool calls
        ran after the fork point, a state-check thought is injected so
        the model re-verifies before acting on stale observations."""
        def do():
            root = self._need_root()
            c, tool_note = chats.fork_chat(root, str(chat_id),
                                           int(upto_index))
            cfg = self._config_or_error()
            try:
                if not cfg.get("error"):
                    c["context"] = chat.context_breakdown(root, cfg, c)
            except Exception:
                pass
            c.setdefault("context", None)
            c["totalMessages"] = len(c.get("messages") or [])
            return {"chat": c, "toolNote": tool_note}
        return _api_call(do)()

    def chat_delete_message(self, chat_id, index, part="message"):
        """Remove one message from the history (the hover bar's delete;
        `index` is absolute). An assistant's tool results leave with it;
        a tool result leaves the calling turn's tool_calls;
        part="thinking" removes just the thought. Refused while a
        response is streaming."""
        def do():
            cid = str(chat_id)
            if chat.is_running(cid):
                raise chats.ChatError(
                    "wait for the response to finish first")
            n = chats.delete_message(self._need_root(), cid, int(index),
                                     str(part or "message"))
            return {"deleted": n}
        return _api_call(do)()

    def chat_get(self, chat_id, tail=None):
        """The chat, optionally windowed to the last `tail` messages -
        with long histories the frontend renders (and transfers) only a
        window; `totalMessages` tells it how much more exists."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            # context breakdown from the FULL history (before windowing)
            context = None
            try:
                context = chat.context_breakdown(root, libconfig.load(root), c)
            except Exception:
                pass   # a broken loom.yaml must not block opening the chat
            total = len(c.get("messages") or [])
            if tail:
                n = max(1, int(tail))
                if n < total:
                    c = dict(c)
                    c["messages"] = c["messages"][-n:]
            c["totalMessages"] = total
            c["context"] = context
            return {"chat": c, "running": chat.is_running(str(chat_id))}
        return _api_call(do)()

    def chat_diag(self, chat_id):
        """Per-entry token accounting for the context diagnostics view:
        one row per user message / assistant reply / thinking block /
        tool result / compaction marker, in chronological order, with
        the same chars/4 estimate the context chip uses."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            est = chat._est
            rows = []

            def prev(text, n=160):
                t = " ".join(str(text or "").split())
                return t[:n]

            # durations: an entry's ts marks when it LANDED; the gap from
            # the previous entry is how long it took. Assistant turns
            # prefer llama's real timings; user messages count as instant
            # (their gap is the human being away, not context cost).
            prev_ts = c.get("createdTs") or 0
            for i, m in enumerate(c.get("messages") or []):
                role = m.get("role")
                ts = m.get("ts") or 0
                delta = max(0, ts - prev_ts) if ts and prev_ts else 0
                if ts:
                    prev_ts = ts
                if role == "user":
                    rows.append({"i": i, "kind": "user", "ts": ts,
                                 "durMs": 0,
                                 "tokens": est(m.get("content")),
                                 "preview": prev(m.get("content")),
                                 "extra": (str(len(m.get("images") or []))
                                           + " image(s)") if m.get("images") else ""})
                elif role == "assistant":
                    t = m.get("timings") or {}
                    u = m.get("usage") or {}
                    dur = int((t.get("prompt_ms") or 0)
                              + (t.get("predicted_ms") or 0)) or delta
                    calls = m.get("tool_calls") or []
                    think_tok = est(m.get("thinking")) if m.get("thinking") else 0
                    body_tok = est(m.get("content")) \
                        + sum(est(json.dumps(tc)) for tc in calls)
                    total_tok = max(1, think_tok + body_tok)
                    # the REAL numbers the server reported for this turn -
                    # the table/tiles prefer these over chars/4 estimates
                    real = {
                        "tps": float(t.get("predicted_per_second") or 0),
                        "promptTps": float(t.get("prompt_per_second") or 0),
                        "promptMs": float(t.get("prompt_ms") or 0),
                        "genMs": float(t.get("predicted_ms") or 0),
                        "promptN": int(t.get("prompt_n") or 0),
                        "cacheN": int(t.get("cache_n") or 0),
                        "outN": int(t.get("predicted_n")
                                    or u.get("completion_tokens") or 0),
                        "ctxTokens": int(u.get("prompt_tokens") or 0)
                        + int(u.get("completion_tokens") or 0),
                        "ttftMs": int(m.get("ttftMs") or 0),
                    } if (t or u) else None
                    if m.get("thinking"):
                        # one generation produced both - split its time
                        # proportionally to the tokens each part got
                        rows.append({"i": i, "kind": "think", "ts": ts,
                                     "durMs": dur * think_tok // total_tok,
                                     "tokens": think_tok,
                                     "preview": prev(m.get("thinking")),
                                     "extra": ""})
                    rows.append({"i": i, "kind": "assistant", "ts": ts,
                                 "durMs": dur - (dur * think_tok // total_tok),
                                 "tokens": body_tok,
                                 "real": real,
                                 "preview": prev(m.get("content"))
                                 or ("→ " + ", ".join(
                                     tc.get("function", {}).get("name", "?")
                                     for tc in calls)),
                                 "extra": (str(len(calls)) + " tool call(s)")
                                 if calls else ""})
                elif role == "tool":
                    mark = (" · CANCELLED" if m.get("cancelled")
                            else "" if m.get("ok", True) else " · FAILED")
                    rows.append({"i": i, "kind": "tool", "ts": ts,
                                 "durMs": delta,
                                 "tokens": est(m.get("content")),
                                 "preview": prev(m.get("content")),
                                 "extra": (m.get("name") or "tool") + mark})
                elif role == "compact":
                    rows.append({"i": i, "kind": "compact", "ts": ts,
                                 "durMs": m.get("durMs") or delta,
                                 "tokens": est(m.get("content")),
                                 "preview": prev(m.get("content")),
                                 "extra": str(m.get("replaced") or 0)
                                 + " messages summarized"})
            nctx = 0
            model_label = ""
            try:
                cfg = libconfig.load(root)
                nctx = chat._nctx_for(cfg, c)
                ep = chat.resolve_endpoint(cfg, c)
                model_label = ep["name"]
            except (libconfig.ConfigError, chats.ChatError):
                pass
            return {"rows": rows, "title": c.get("title") or "Chat",
                    "nCtx": nctx, "model": model_label}
        return _api_call(do)()

    def chat_diag_export(self, chat_id):
        """Save a metadata/stats-only JSON snapshot of the chat: per-entry
        token estimates, the server's REAL usage/timings objects, context
        breakdown, and compaction settings - NO message or response text.
        Made for pasting into a bug report or handing to an assistant."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            cfg, context, nctx = None, None, 0
            try:
                cfg = libconfig.load(root)
                nctx = chat._nctx_for(cfg, c)
                context = chat.context_breakdown(root, cfg, c)
            except Exception:
                pass   # a broken loom.yaml still exports the entry stats
            est = chat._est
            entries = []
            for i, m in enumerate(c.get("messages") or []):
                role = m.get("role")
                e = {"i": i, "role": role, "ts": m.get("ts"),
                     "chars": len(str(m.get("content") or "")),
                     "estTokens": est(m.get("content"))}
                if role == "user" and m.get("images"):
                    e["images"] = len(m["images"])
                elif role == "assistant":
                    if m.get("thinking"):
                        e["thinkingChars"] = len(str(m["thinking"]))
                        e["estThinkTokens"] = est(m["thinking"])
                    if m.get("tool_calls"):
                        e["toolCalls"] = [
                            tc.get("function", {}).get("name", "?")
                            for tc in m["tool_calls"]]
                        e["estToolCallTokens"] = sum(
                            est(json.dumps(tc)) for tc in m["tool_calls"])
                    if isinstance(m.get("usage"), dict):
                        e["usage"] = m["usage"]
                    if isinstance(m.get("timings"), dict):
                        e["timings"] = m["timings"]
                    if m.get("ttftMs"):
                        e["ttftMs"] = m["ttftMs"]
                    if m.get("stopped"):
                        e["stopped"] = True
                elif role == "tool":
                    e["tool"] = m.get("name") or "tool"
                    e["ok"] = bool(m.get("ok", True))
                    if m.get("cancelled"):
                        e["cancelled"] = True
                elif role == "compact":
                    for k in ("replaced", "omitted", "tokensBefore", "durMs"):
                        if m.get(k) is not None:
                            e[k] = m[k]
                entries.append(e)
            doc = {"exportedAt": int(time.time() * 1000),
                   "chatId": c.get("id"), "title": c.get("title"),
                   "createdTs": c.get("createdTs"),
                   "provider": c.get("provider")
                   or ((cfg or {}).get("chat") or {}).get("provider") or "",
                   "model": c.get("model")
                   or ((cfg or {}).get("chat") or {}).get("model") or "",
                   "nCtx": nctx,
                   "compaction": ((cfg or {}).get("chat") or {}).get("compaction"),
                   "context": context, "totalMessages": len(entries),
                   "entries": entries}
            if self._window is None:
                raise chats.ChatError("window not ready")
            suggest = f"loom-diag-{str(c.get('id'))[:8]}.json"
            got = self._window.create_file_dialog(
                webview.FileDialog.SAVE, save_filename=suggest)
            if not got:
                return {"saved": None}
            dest = Path(got[0] if isinstance(got, (list, tuple)) else got)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            return {"saved": str(dest)}
        return _api_call(do)()

    # -------- diagnostics pop-out: a real OS window for the diag view --------
    def diag_popout(self, chat_id):
        """Open (or focus) a separate window carrying this chat's
        diagnostics. The window shares the bridge api and the event bus,
        so it stays live; it dies with the main window."""
        def do():
            cid = str(chat_id)
            key = f"diag:{cid}"
            existing = child_window(key)
            if existing is not None:
                _qt_focus(existing)
                return {"focused": True}
            title = "Diagnostics"
            try:
                c = chats.load_chat(self._need_root(), cid)
                title = "Diagnostics · " + (c.get("title") or "Chat")
            except (chats.ChatError, library.LibraryError):
                pass
            win = webview.create_window(
                title,
                url=compose.DIAG_OUTPUT.as_uri() + "#chat=" + cid,
                js_api=self,
                width=1000, height=720, min_size=(720, 480))
            register_child_window(key, win, self._bus)
            return {}
        return _api_call(do)()

    def diag_popin(self, chat_id):
        """The child window's 'Return to app' button: close the window
        and reopen the diagnostics as a tab in the main window."""
        def do():
            cid = str(chat_id)
            win = pop_child_window(f"diag:{cid}", self._bus)
            self._bus.push({"type": "diag_popin", "chatId": cid})
            _qt_focus(self._window)
            if win is not None:
                # destroy LAST - this call rides the dying window's own
                # bridge, so the reply must already be on its way
                def kill():
                    time.sleep(0.15)
                    try:
                        win.destroy()
                    except Exception:
                        pass
                threading.Thread(target=kill, daemon=True,
                                 name="diag-popin").start()
            return {}
        return _api_call(do)()

    def chat_compact(self, chat_id):
        """Manual 'Compact now' - same machinery as auto-compaction."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            if not c.get("messages"):
                raise chats.ChatError("nothing to compact yet")
            chat.start_compaction(root, str(chat_id), self._bus.push)
            return {}
        return _api_call(do)()

    def chat_unarchive(self, chat_id):
        return _api_call(lambda: {"chat": chats.set_archived(
            self._need_root(), str(chat_id), False)})()

    def chat_close(self, chat_id, force=False):
        """Close a chat tab → archive it. A chat with inference running
        refuses unless force (the frontend confirms cancellation first).
        An EMPTY chat (no messages) is deleted instead - never archived."""
        def do():
            cid = str(chat_id)
            if chat.is_running(cid):
                if not force:
                    return {"ok": False, "error": "inference is running",
                            "needsConfirm": True}
                chat.stop(cid)
                chat.join_worker(cid)
            root = self._need_root()
            c = chats.load_chat(root, cid)
            if not c.get("messages"):
                chats.delete_chat(root, cid)
                return {"deleted": True}
            chats.set_archived(root, cid, True)
            return {"archived": True}
        return _api_call(do)()

    def chat_delete(self, chat_id):
        def do():
            cid = str(chat_id)
            if chat.is_running(cid):
                chat.stop(cid)
                chat.join_worker(cid)   # its final save must not
                                        # resurrect the file we remove
            chats.delete_chat(self._need_root(), cid)
            # a mirror terminal has nothing left to mirror
            win = pop_child_window(f"cterm:{cid}", self._bus)
            def drop():
                if win is not None:
                    try:
                        win.destroy()
                    except Exception:
                        pass
                chatterm.close_for_chat(cid)
            threading.Thread(target=drop, daemon=True,
                             name=f"cterm-drop-{cid}").start()
            return {}
        return _api_call(do)()

    def chat_set_title(self, chat_id, title):
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            t = str(title or "").strip()
            if not t:
                raise chats.ChatError("the title cannot be empty")
            c["title"] = t[:80]
            chats.save_chat(self._need_root(), c)
            return {"title": c["title"]}
        return _api_call(do)()

    def chat_set_model(self, chat_id, provider, model):
        """Both halves of the choice at once - a model id only means
        something within its provider."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            c["provider"] = str(provider or "")
            c["model"] = str(model or "")
            chats.save_chat(self._need_root(), c)
            return {}
        return _api_call(do)()

    def chat_set_container(self, chat_id, container):
        """Which container definition runs this chat's shell commands.
        Empty = the loom.yaml containers.default."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            name = str(container or "")
            if name:
                cfg = libconfig.load(root)
                defs = [d["name"] for d in
                        (cfg.get("containers") or {}).get("definitions") or []]
                if name not in defs:
                    raise chats.ChatError(f"no container named {name!r}")
            c["container"] = name
            chats.save_chat(root, c)
            chatterm.sync_async(self._bus.push, root, c["id"])
            return {"container": name}
        return _api_call(do)()

    def chat_set_env(self, chat_id, env_name):
        """Which environment (env-var set) loads into this chat's shell
        containers. Empty = none. A USER decision only."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            name = str(env_name or "")
            if name and name not in envs.list_envs(root):
                raise chats.ChatError(f"no environment named {name!r}")
            c["env"] = name
            chats.save_chat(root, c)
            chatterm.sync_async(self._bus.push, root, c["id"])
            return {"env": name}
        return _api_call(do)()

    # -------- environments: definitions (and secret STUBS) live in the
    # library's environments.yaml; secret VALUES live in the OS keyring --------
    def envs_list(self):
        def do():
            try:
                return {"envs": envs.list_envs(self._need_root())}
            except envs.EnvError as e:
                raise chats.ChatError(str(e))
        return _api_call(do)()

    def env_get(self, name):
        """One environment for the editor: rows plus which secret stubs
        have a value in THIS machine's keyring. Secret VALUES never
        cross the bridge."""
        def do():
            root = self._need_root()
            try:
                defs = envs.read_defs(root)
                n = str(name)
                if n not in defs:
                    raise chats.ChatError(f"no environment named {n!r}")
                status = envs.secret_status(root, n)
                rows = [{"key": k, "secret": v is None,
                         "value": "" if v is None else v,
                         "hasSecret": bool(status.get(k))}
                        for k, v in sorted(defs[n].items())]
                return {"vars": rows}
            except envs.EnvError as e:
                raise chats.ChatError(str(e))
        return _api_call(do)()

    def env_save(self, name, variables, secret_values):
        def do():
            try:
                return {"envs": envs.save_env(
                    self._need_root(), str(name),
                    variables if isinstance(variables, list) else [],
                    secret_values if isinstance(secret_values, dict) else {})}
            except envs.EnvError as e:
                raise chats.ChatError(str(e))
        return _api_call(do)()

    def env_delete(self, name):
        def do():
            try:
                return {"envs": envs.delete_env(self._need_root(), str(name))}
            except envs.EnvError as e:
                raise chats.ChatError(str(e))
        return _api_call(do)()

    def chat_set_artifacts(self, chat_id, on):
        """The artifacts chip: enable/disable artifact DELIVERY for the
        chat. Off = the deliver_artifact tool is not offered (and
        refused if called anyway); already-delivered artifacts stay
        viewable. Containers are untouched - artifacts were never a
        mount."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            if on:
                c.pop("artifactsOff", None)
            else:
                c["artifactsOff"] = True
            chats.save_chat(self._need_root(), c)
            return {"artifacts": not c.get("artifactsOff")}
        return _api_call(do)()

    def chat_set_thought_truncation(self, chat_id, truncate):
        """The thoughts chip: per-chat thought truncation. True = only
        the latest turn's thinking rides the wire (the default);
        False = every stored thought does. loom.yaml's
        chat.thought_truncation only seeds NEW chats."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            c["thoughtTruncation"] = bool(truncate)
            chats.save_chat(self._need_root(), c)
            return {"thoughtTruncation": c["thoughtTruncation"]}
        return _api_call(do)()

    def chat_set_mcp_perm(self, chat_id, tool, level):
        """Per-CHAT permission override for one MCP tool (the tools
        bar's MCP panel). Wins over loom.yaml's per-mode entries and the
        MCP Servers tab's defaults; empty level clears the override."""
        def do():
            t = str(tool or "").strip()
            lv = str(level or "").strip().lower()
            if not t.startswith("mcp_"):
                raise chats.ChatError("that is not an MCP tool")
            if lv and lv not in libconfig.PERM_LEVELS:
                raise chats.ChatError(
                    "the level must be one of "
                    + ", ".join(libconfig.PERM_LEVELS) + " (or empty)")
            c = chats.load_chat(self._need_root(), str(chat_id))
            perms = dict(c.get("mcpPerms") or {})
            if lv:
                perms[t] = lv
            else:
                perms.pop(t, None)
            if perms:
                c["mcpPerms"] = perms
            else:
                c.pop("mcpPerms", None)
            chats.save_chat(self._need_root(), c)
            return {"mcpPerms": perms}
        return _api_call(do)()

    def chat_set_env_hidden(self, chat_id, names):
        """Per-chat env SIGNAL hiding (the tools bar's env panel): the
        named variables still load into shell containers - the model is
        just never told they exist."""
        def do():
            clean = sorted({str(n).strip() for n in names
                            if isinstance(names, list) and str(n).strip()}) \
                if isinstance(names, list) else []
            c = chats.load_chat(self._need_root(), str(chat_id))
            if clean:
                c["envHidden"] = clean
            else:
                c.pop("envHidden", None)
            chats.save_chat(self._need_root(), c)
            return {"envHidden": clean}
        return _api_call(do)()

    def chat_set_knowledge(self, chat_id, on):
        """The tools bar's knowledge chip: OFF cuts the knowledge base
        out of the chat entirely - the knowledge_search tool vanishes,
        /knowledge is refused by file tools and not mounted in shells,
        and the system prompt stops describing (or even mentioning) the
        base and its contents."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            if on:
                c.pop("knowledgeOff", None)
            else:
                c["knowledgeOff"] = True
            chats.save_chat(self._need_root(), c)
            chatterm.sync_async(self._bus.push, self._need_root(), c["id"])
            return {"knowledge": not c.get("knowledgeOff")}
        return _api_call(do)()

    def chat_set_time_signals(self, chat_id, on):
        """The time-travel panel's master toggle. Signals OFF = the model
        receives no datetime signal at all: no session-start stamp, no
        [brackets] on user or assistant messages. Time travel offsets
        stay armed and recorded; they just have nothing to show."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            if on:
                c.pop("timeSignalsOff", None)
            else:
                c["timeSignalsOff"] = True
            chats.save_chat(self._need_root(), c)
            return {"timeSignals": not c.get("timeSignalsOff")}
        return _api_call(do)()

    def chat_set_time_travel(self, chat_id, offset_ms):
        """Arm (or, with 0, clear) the chat's TIME TRAVEL offset: every
        user message sent from now on is signalled to the model as
        real-time + offset - a probe of the model's signal awareness.
        Already-sent messages keep the stamps they reported."""
        def do():
            off = int(offset_ms or 0)
            limit = 3650 * 24 * 3600 * 1000   # ±10 years is plenty
            if not (-limit <= off <= limit):
                raise chats.ChatError(
                    "the time-travel offset must be within ±10 years")
            c = chats.load_chat(self._need_root(), str(chat_id))
            if off:
                c["timeTravelMs"] = off
            else:
                c.pop("timeTravelMs", None)
            chats.save_chat(self._need_root(), c)
            return {"offsetMs": off}
        return _api_call(do)()

    def chat_set_network(self, chat_id, mode):
        """Per-chat container network mode: none / loopback / on
        (legacy booleans accepted). Off by default; a USER decision only
        - nothing in a library can change it."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            c["network"] = containers.net_mode(mode)
            chats.save_chat(self._need_root(), c)
            chatterm.sync_async(self._bus.push, self._need_root(), c["id"])
            return {"network": c["network"]}
        return _api_call(do)()

    def chat_set_folders(self, chat_id, folders):
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            clean = self._clean_folders(folders)
            c["folders"] = clean
            chats.save_chat(self._need_root(), c)
            chatterm.sync_async(self._bus.push, self._need_root(), c["id"])
            return {"folders": clean}
        return _api_call(do)()

    def drop_paths(self):
        """Native filesystem paths of the OS drag-drop that just landed.
        The qt backend records them (we bump its listener count at boot -
        see main()); JS drop events can't see real paths on their own.
        Draining here keeps stale drops from accumulating."""
        def do():
            import urllib.parse as _up
            try:
                from webview.dom import _dnd_state
                out = [_up.unquote(str(p[1]))
                       for p in _dnd_state.get("paths", [])]
                _dnd_state["paths"] = []
            except Exception:
                out = []
            return {"paths": out}
        return _api_call(do)()

    def path_kind(self, path):
        """dir / file / missing - lets the composer's drop handler tell a
        FOLDER drop (attach as a mount) from an image drop."""
        def do():
            p = Path(str(path or "")).expanduser()
            kind = "dir" if p.is_dir() else "file" if p.is_file() else "missing"
            return {"kind": kind}
        return _api_call(do)()

    def git_info(self, paths):
        """{path: branch} for the attached folders that are git worktrees -
        polled by the frontend so the pills track checkouts made outside
        the app. Cheap: reads .git/HEAD, never runs git."""
        def do():
            out = {}
            for p in paths if isinstance(paths, list) else []:
                try:
                    b = library.git_branch(Path(str(p)).expanduser())
                except (OSError, RuntimeError):
                    b = None
                if b:
                    out[str(p)] = b
            return {"branches": out}
        return _api_call(do)()

    def stage_image(self, name, data_url):
        """Drag-and-dropped image bytes → a file under ~/.loom/attachments
        (browser drops carry no filesystem path in this webview build).
        The path persists - chats re-encode it on every send."""
        def do():
            import base64
            import re as _re
            import uuid as _uuid
            m = _re.match(r"^data:([\w/+.-]+);base64,(.*)$",
                          str(data_url or ""), _re.S)
            if not m:
                raise chats.ChatError("that drop was not an image")
            raw = base64.b64decode(m.group(2))
            if len(raw) > 30_000_000:
                raise chats.ChatError("image too large (30 MB max)")
            safe = _re.sub(r"[^\w.-]", "_", str(name or "image"))[:80] or "image"
            d = store.home() / "attachments"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{_uuid.uuid4().hex[:8]}-{safe}"
            p.write_bytes(raw)
            return {"path": str(p)}
        return _api_call(do)()

    def chat_send(self, chat_id, text, images=None):
        def do():
            root = self._need_root()
            cid = str(chat_id)
            # a stream the user just cancelled may still be unwinding -
            # wait that out instead of bouncing the send
            if chat.is_running(cid) and not chat.wait_if_cancelling(cid):
                raise chats.ChatError("a response is already streaming")
            c = chats.load_chat(root, cid)
            msg = {"role": "user", "content": str(text or ""),
                   "ts": __import__("time").time_ns() // 1_000_000}
            off = int(c.get("timeTravelMs") or 0)
            if off:
                # time travel armed: the model is SIGNALLED this shifted
                # time; the real ts stays the record (and the UI's)
                msg["signalTs"] = msg["ts"] + off
            imgs = []
            for p in images if isinstance(images, list) else []:
                if str(p).strip():
                    imgs.append({"path": str(p)})
            if imgs:
                msg["images"] = imgs
                self._stage_uploads(root, cid, [i["path"] for i in imgs])
            c["messages"].append(msg)
            chats.save_chat(root, c)
            chat.send(root, cid, self._bus.push)
            return {"message": msg}
        return _api_call(do)()

    @staticmethod
    def _stage_uploads(root, chat_id, paths):
        """Copy the user's attachments into the chat's uploads dir
        (/uploads in containers, read-only) so shell/file tools can
        reach them. Best-effort - a failed copy must not block the send."""
        import shutil as _sh
        import uuid as _uuid
        updir = chats.artifacts_dir(root, str(chat_id), create=True) / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        for p in paths:
            try:
                src = Path(str(p)).expanduser()
                if not src.is_file():
                    continue
                dst = updir / src.name
                if dst.exists():
                    if dst.stat().st_size == src.stat().st_size:
                        continue          # same upload resent
                    dst = updir / f"{_uuid.uuid4().hex[:6]}-{src.name}"
                _sh.copy2(src, dst)
            except OSError:
                continue

    def _artifact_path(self, chat_id, name) -> Path:
        """The artifact's real path, escape-proofed."""
        root = self._need_root()
        base = chats.artifacts_dir(root, str(chat_id))
        src = (base / str(name)).resolve()
        if src != base and base not in src.parents:
            raise chats.ChatError(f"bad artifact name: {name}")
        return src

    # 5 MB: past that the "editor" experience is misery anyway - the
    # window degrades to download-only
    ARTIFACT_EDIT_MAX = 5_000_000

    _ARTIFACT_IMG_RE = None

    def artifact_open(self, chat_id, name):
        """Open (or focus) a preview/editor window for one artifact.
        Text opens in the real editor (saving writes the artifact in
        place, so the model sees the changes); images render; anything
        else gets a download button. The window closes with the app."""
        def do():
            cid = str(chat_id)
            src = self._artifact_path(cid, name)
            if not src.exists():
                raise chats.ChatError(f"no such artifact: {name}")
            key = f"art:{cid}:{name}"
            existing = child_window(key)
            if existing is not None:
                _qt_focus(existing)
                return {"focused": True}
            import urllib.parse as _up
            win = webview.create_window(
                f"{src.name} · artifact",
                url=compose.ART_OUTPUT.as_uri()
                    + "#chat=" + _up.quote(cid)
                    + "&name=" + _up.quote(str(name)),
                js_api=self,
                width=940, height=700, min_size=(560, 400))
            register_child_window(key, win, self._bus)
            return {}
        return _api_call(do)()

    def artifact_read(self, chat_id, name):
        """What the preview window shows: text (editable), an image
        path, or 'binary'/'dir' (download only)."""
        def do():
            src = self._artifact_path(str(chat_id), name)
            if not src.exists():
                raise chats.ChatError(f"no such artifact: {name}")
            out = {"name": src.name, "rel": str(name)}
            if src.is_dir():
                return {**out, "kind": "dir", "bytes": 0}
            size = src.stat().st_size
            out["bytes"] = size
            import re as _re
            if type(self)._ARTIFACT_IMG_RE is None:
                type(self)._ARTIFACT_IMG_RE = _re.compile(
                    r"\.(png|jpe?g|webp|gif|bmp|svg)$", _re.I)
            if type(self)._ARTIFACT_IMG_RE.search(src.name):
                return {**out, "kind": "image", "path": str(src)}
            if size > self.ARTIFACT_EDIT_MAX:
                return {**out, "kind": "binary"}
            data = src.read_bytes()
            if library.looks_binary(data):
                return {**out, "kind": "binary"}
            return {**out, "kind": "text",
                    "text": data.decode("utf-8", "replace")}
        return _api_call(do)()

    def artifact_write(self, chat_id, name, text):
        """The preview window's Save: write the artifact IN PLACE. The
        file lives in the chat's /artifacts folder, so the model reads
        the edited version on its next tool call. The stored artifact
        records refresh too, so the user's own edit is not re-announced
        as a fresh delivery."""
        def do():
            root = self._need_root()
            cid = str(chat_id)
            src = self._artifact_path(cid, name)
            if not src.is_file():
                raise chats.ChatError(f"no such artifact file: {name}")
            src.write_text(str(text or ""), encoding="utf-8")
            try:
                c = chats.load_chat(root, cid)
                c["artifacts"] = chat._artifact_records(root, cid)
                chats.save_chat(root, c)
            except chats.ChatError:
                pass   # the write itself succeeded
            return {"bytes": src.stat().st_size}
        return _api_call(do)()

    def artifact_dismiss(self, chat_id, name):
        """Clear one artifact pill from the attach bar. The file stays,
        and the timeline message keeps its own open/save buttons; a
        fresh regeneration brings the pill back."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            names = {a.get("name") for a in c.get("artifacts") or []}
            n = str(name)
            if n not in names:
                raise chats.ChatError(f"no such artifact: {name}")
            dis = [x for x in c.get("artifactsDismissed") or [] if x != n]
            dis.append(n)
            c["artifactsDismissed"] = dis
            chats.save_chat(root, c)
            return {"dismissed": dis}
        return _api_call(do)()

    def artifact_save(self, chat_id, name):
        """Save one artifact where the user chooses: files copy, folders
        zip. Backed by the /artifacts pills in the chat's attach bar."""
        def do():
            import shutil as _sh
            root = self._need_root()
            base = chats.artifacts_dir(root, str(chat_id))
            src = self._artifact_path(str(chat_id), name)
            if not src.exists():
                raise chats.ChatError(f"no such artifact: {name}")
            if self._window is None:
                raise chats.ChatError("window not ready")
            suggest = src.name + (".zip" if src.is_dir() else "")
            got = self._window.create_file_dialog(
                webview.FileDialog.SAVE, save_filename=suggest)
            if not got:
                return {"saved": None}
            dest = Path(got[0] if isinstance(got, (list, tuple)) else got)
            if src.is_dir():
                stem = str(dest)
                if stem.lower().endswith(".zip"):
                    stem = stem[:-4]
                out = _sh.make_archive(stem, "zip",
                                       root_dir=str(base), base_dir=src.name)
                return {"saved": out}
            dest.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(src, dest)
            return {"saved": str(dest)}
        return _api_call(do)()

    # -------- pinned models (pickers list these first) --------
    def pins_all(self):
        return _api_call(lambda: {
            "pins": store.pins_all(str(self._need_root()))})()

    def pin_set(self, model_id, pinned):
        return _api_call(lambda: {
            "pins": store.pin_set(str(self._need_root()), str(model_id),
                                  bool(pinned))})()

    # -------- per-model reasoning preferences --------
    def reasoning_all(self):
        return _api_call(lambda: {
            "reasoning": store.reasoning_all(str(self._need_root()))})()

    def reasoning_set(self, model_id, pref):
        """pref = {method, level} - or None/{} to restore the default
        (send nothing; the server decides)."""
        def do():
            try:
                store.reasoning_set(str(self._need_root()), str(model_id),
                                    pref if isinstance(pref, dict) and pref
                                    else None)
            except ValueError as e:
                raise chats.ChatError(str(e))
            return {"reasoning": store.reasoning_all(str(self._need_root()))}
        return _api_call(do)()

    def chat_continue(self, chat_id):
        """Run the loop on the chat AS IS - no new user message. Backs
        the Retry button (last message is the user's), the Continue
        button (the model stopped abruptly), and Ctrl+R on any ending -
        including an EMPTY chat, where the model opens the conversation
        from the system prompt alone."""
        def do():
            cid = str(chat_id)
            if chat.is_running(cid) and not chat.wait_if_cancelling(cid):
                raise chats.ChatError("a response is already streaming")
            chats.load_chat(self._need_root(), cid)   # must exist
            chat.send(self._need_root(), cid, self._bus.push)
            return {}
        return _api_call(do)()

    def chat_set_mode(self, chat_id, mode):
        def do():
            root = self._need_root()
            cfg = libconfig.load(root)
            m = str(mode or "")
            if m and m not in cfg["permissionModes"]:
                raise chats.ChatError(f"unknown permission mode: {m}")
            c = chats.load_chat(root, str(chat_id))
            c["permMode"] = m
            chats.save_chat(root, c)
            # tool calls already waiting at the gate are RE-DECIDED under
            # the new mode: now-allowed calls proceed, now-denied ones are
            # refused; "ask" keeps waiting for the user
            for call_id, tool in chat.GATE.pending_for(str(chat_id)):
                lvl = libconfig.permission_for(cfg, tool, m)
                if lvl == "allow":
                    chat.GATE.answer(call_id, "allow")
                elif lvl in ("deny", "disabled"):
                    chat.GATE.answer(call_id, "deny")
            return {"mode": m}
        return _api_call(do)()

    def chat_stop(self, chat_id):
        return _api_call(lambda: {"stopped": chat.stop(str(chat_id))})()

    def tool_answer(self, call_id, decision):
        return _api_call(lambda: {"answered": chat.GATE.answer(
            str(call_id), "allow" if decision == "allow" else "deny")})()

    # ---------------- terminals ----------------
    def term_open(self, opts):
        def do():
            root = self._need_root()
            o = opts if isinstance(opts, dict) else {}
            tab_id = str(o.get("tabId") or "")
            if not tab_id:
                raise terminals.TermError("terminal tab id missing")
            folders = self._clean_folders(o.get("folders"))

            def work():
                terminals.open_session(
                    self._bus.push, root, tab_id,
                    str(o.get("container") or ""), folders,
                    containers.net_mode(o.get("network")),
                    int(o.get("cols") or 120), int(o.get("rows") or 32),
                    env_name=str(o.get("env") or ""))
            threading.Thread(target=work, daemon=True,
                             name=f"term-open-{tab_id}").start()
            return {}
        return _api_call(do)()

    def term_attach(self, tab_id, cols, rows):
        return _api_call(lambda: {"attached": terminals.attach(
            self._bus.push, str(tab_id), int(cols or 120), int(rows or 32))})()

    def term_write(self, tab_id, data):
        ok = terminals.write(str(tab_id), str(data))
        return {"ok": ok} if ok else {"ok": False, "error": "no live shell"}

    def term_resize(self, tab_id, cols, rows):
        return _api_call(lambda: {"resized": terminals.resize(
            str(tab_id), int(cols or 120), int(rows or 32))})()

    def term_procs(self, tab_id):
        return _api_call(lambda: {"procs": terminals.procs(str(tab_id))})()

    def term_cleanup(self, tab_id):
        def do():
            threading.Thread(target=lambda: terminals.cleanup(str(tab_id)),
                             daemon=True, name="term-cleanup").start()
            return {}
        return _api_call(do)()

    # -------- the chat-mirror terminal: a shell in the chat's exact
    # container setup, popped out into its own window --------
    def chat_term_popout(self, chat_id):
        """Open (or focus) a separate window with a terminal running in
        this chat's exact container view (image, /mnt mounts, the
        /knowledge cut, /uploads, the chat's /home/loom, network,
        environment). The shell dies with the window - and restarts
        itself whenever the chat's setup changes."""
        def do():
            root = self._need_root()
            cid = str(chat_id)
            c = chats.load_chat(root, cid)   # bad ids fail HERE, visibly
            key = f"cterm:{cid}"
            existing = child_window(key)
            if existing is not None:
                _qt_focus(existing)
                return {"focused": True}
            win = webview.create_window(
                "Terminal · " + (c.get("title") or "Chat"),
                url=compose.TERM_OUTPUT.as_uri() + "#chat=" + cid,
                js_api=self,
                width=1000, height=680, min_size=(640, 400))
            register_child_window(key, win, self._bus)

            def on_closed():
                # the mirror shell has no life outside its window; kill
                # off-thread - closed fires inside Qt's loop
                threading.Thread(
                    target=lambda: chatterm.close_for_chat(cid),
                    daemon=True, name=f"cterm-close-{cid}").start()
            win.events.closed += on_closed
            return {}
        return _api_call(do)()

    def chat_term_open(self, chat_id, cols, rows):
        """The child window's session start/restart. The setup always
        comes fresh off the chat document - never from the page."""
        def do():
            root = self._need_root()
            cid = str(chat_id)
            chats.load_chat(root, cid)   # validate before the thread
            threading.Thread(
                target=lambda: chatterm.open_for_chat(
                    self._bus.push, root, cid,
                    int(cols or 120), int(rows or 32)),
                daemon=True, name=f"cterm-open-{cid}").start()
            return {"sid": chatterm.sid_for(cid)}
        return _api_call(do)()

    def chat_term_info(self, chat_id):
        """The chat's container view, for the child window's read-only
        header - what the mirror shell is (or would be) running."""
        def do():
            root = self._need_root()
            c = chats.load_chat(root, str(chat_id))
            name = str(c.get("container") or "")
            if not name:
                cfg = libconfig.load(root)
                name = str((cfg.get("containers") or {}).get("default")
                           or "sandbox")
            return {"title": c.get("title") or "Chat",
                    "sid": chatterm.sid_for(str(chat_id)),
                    "container": name,
                    "folders": [{"path": str(f.get("path") or ""),
                                 "mode": str(f.get("mode") or "view")}
                                for f in c.get("folders") or []],
                    "network": containers.net_mode(c.get("network")),
                    "env": str(c.get("env") or ""),
                    "knowledge": not c.get("knowledgeOff"),
                    "uploads": (chats.artifacts_dir(root, str(chat_id))
                                / "uploads").is_dir()}
        return _api_call(do)()

    # ---------------- switching libraries ----------------
    def switch_blockers(self):
        """What switching away interrupts: chats mid-generation and live
        terminals. Providers are other people's processes - they keep
        running, untouched."""
        def do():
            return {"chats": len(chat.running_chats()),
                    "terminals": terminals.live_count()}
        return _api_call(do)()

    def library_close(self):
        """Leave the current library: cancel streaming chats, close its
        terminals and MCP servers. Providers stay up - not ours."""
        def do():
            # cancel AND JOIN the workers BEFORE the lock releases:
            # a dying worker's final save (the partial turn) must land
            # while this process still owns the library - releasing
            # first would let another instance grab the lock mid-write.
            # The joins share one bounded budget so a wedged worker
            # can't hold the switch hostage.
            for cid in chat.running_chats():
                chat.stop(cid)
            deadline = time.monotonic() + 5.0
            for cid in chat.running_chats():
                chat.join_worker(cid, max(0.5,
                                          deadline - time.monotonic()))
            library.release_lock(self._root)
            self._root = None
            _set_tray_tooltip("Loom")
            close_child_windows(self._bus)   # they show THIS library's chats
            apiserver.stop()   # the API routes THIS library's providers
            self._bus.push({"type": "apisrv", **apiserver.status()})
            mcp.shutdown()     # and the MCP servers are its config too
            # terminals run the library's containers - they close with it
            threading.Thread(target=terminals.shutdown, daemon=True,
                             name="lib-close-terms").start()
            providers.forget_all()
            return {}
        return _api_call(do)()

    # ---------------- MCP servers ----------------
    def _mcp_cfg(self) -> list[dict]:
        cfg = libconfig.load(self._need_root())
        return cfg.get("mcpServers") or []

    def mcp_status(self):
        return _api_call(lambda: {"servers": mcp.status(self._mcp_cfg())})()

    def mcp_toggle(self, name, on):
        """Enable/disable one MCP server. Enabled == running, and the set
        persists - enabled servers autostart when the library reopens."""
        def do():
            rec = next((s for s in self._mcp_cfg()
                        if s["name"] == str(name)), None)
            if rec is None:
                raise mcp.McpError(f"no mcp server named {name!r} in "
                                   "loom.yaml")
            if on:
                mcp.start_server(rec)
            else:
                mcp.stop_server(rec["name"])
            store.set_mcp_running(str(self._need_root()), rec["name"],
                                  bool(on))
            return {"servers": mcp.status(self._mcp_cfg())}
        return _api_call(do)()

    def mcp_refresh(self, name):
        """Re-query one running server's tool list."""
        def do():
            n = mcp.refresh_tools(str(name))
            return {"tools": n, "servers": mcp.status(self._mcp_cfg())}
        return _api_call(do)()

    def mcp_tool_perm_set(self, tool, level):
        """The tab's per-tool DEFAULT permission (loom.yaml per-mode
        entries still win)."""
        def do():
            try:
                store.set_mcp_tool_perm(str(self._need_root()), str(tool),
                                        str(level or ""))
            except ValueError as e:
                raise mcp.McpError(str(e))
            return {"servers": mcp.status(self._mcp_cfg())}
        return _api_call(do)()

    def mcp_add(self, name, command, env):
        """The wizard's final step: inject one `mcp-servers:` entry into
        loom.yaml non-destructively, validating the result first."""
        def do():
            import yaml as _yaml
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            env_map = {str(k): str(v) for k, v in env.items()} \
                if isinstance(env, dict) else {}
            new_text = mcp.inject_server(p.read_text(encoding="utf-8"),
                                         str(name), str(command), env_map)
            try:
                raw = _yaml.safe_load(new_text) or {}
                libconfig._mcp_servers(raw.get("mcp-servers"))
            except _yaml.YAMLError as e:
                raise libconfig.ConfigError(
                    f"the result would not parse - nothing was written: {e}")
            library.write_file(root, p.name, new_text)
            self._bus.push({"type": "config", "config": self._config_or_error()})
            return {"servers": mcp.status(self._mcp_cfg())}
        return _api_call(do)()

    def mcp_update(self, name, new_name, command, env):
        """Rewrite one mcp-servers entry (the card's Edit dialog). A
        running server restarts on the new definition - under the new
        name if renamed."""
        def do():
            old = str(name or "").strip()
            new = str(new_name or "").strip() or old
            env_map = {str(k): str(v) for k, v in env.items()} \
                if isinstance(env, dict) else {}
            new_lines = mcp.entry_lines(new, str(command or ""), env_map)
            cfg = self._config_text_update(
                lambda text: configedit.replace_list_item(
                    text, ("mcp-servers", "mcp_servers"), old, new_lines))
            if old in (mcp.running() or {}):
                mcp.stop_server(old)
                store.set_mcp_running(str(self._need_root()), old, False)
                rec = next((s for s in cfg.get("mcpServers") or []
                            if s["name"] == new), None)
                if rec is not None:
                    mcp.start_server(rec)
                    store.set_mcp_running(str(self._need_root()), new, True)
            return {"servers": mcp.status(cfg.get("mcpServers") or [])}
        return _api_call(do)()

    def mcp_remove(self, name):
        """Delete one mcp-servers entry; a running instance stops first."""
        def do():
            n = str(name or "").strip()
            cfg = self._config_text_update(
                lambda text: configedit.remove_list_item(
                    text, ("mcp-servers", "mcp_servers"), n))
            if n in (mcp.running() or {}):
                mcp.stop_server(n)
            store.set_mcp_running(str(self._need_root()), n, False)
            return {"servers": mcp.status(cfg.get("mcpServers") or [])}
        return _api_call(do)()

    # ---------------- the OpenAI-compatible API server ----------------
    def _api_providers(self) -> list[dict]:
        """Fresh provider list for the API's per-request routing."""
        try:
            if self._root is None:
                return []
            return libconfig.load(self._root).get("providers") or []
        except libconfig.ConfigError:
            return []

    # the API key lives in THIS machine's OS keyring, per library - never
    # in loom.yaml, which is shareable (and often committed) by design
    def _api_key_name(self) -> str:
        return f"loom-api-key::{self._need_root()}"

    def _api_key(self) -> str:
        try:
            return envs._kr_get(self._api_key_name()) or ""
        except Exception:
            return ""   # no library, or no keyring backend: no key

    def api_server_status(self):
        def do():
            cfg = dict(libconfig.DEFAULT_API)
            try:
                if self._root is not None:
                    cfg = libconfig.load(self._root).get("api") or cfg
            except libconfig.ConfigError:
                pass
            return {"api": {**apiserver.status(), "cfg": cfg,
                            "hasKey": bool(self._api_key())}}
        return _api_call(do)()

    def api_server_toggle(self, on):
        """Turn the API on/off. The state is process-local by design -
        every Loom launch starts with the API OFF. A stored key gates
        every request while on."""
        def do():
            if not on:
                apiserver.stop()
            else:
                cfg = libconfig.load(self._need_root()).get("api") \
                    or libconfig.DEFAULT_API
                try:
                    apiserver.start(cfg["interface"], cfg["port"],
                                    self._api_providers, self._api_key())
                except apiserver.ApiError as e:
                    raise libconfig.ConfigError(str(e))
            got = apiserver.status()
            self._bus.push({"type": "apisrv", **got})
            return {"api": {**got, "hasKey": bool(self._api_key())}}
        return _api_call(do)()

    def api_server_key_set(self, key):
        """Set (or, with an empty string, clear) the API key. Stored in
        the OS keyring; a running API restarts so the change applies
        immediately."""
        def do():
            name = self._api_key_name()
            k = str(key or "").strip()
            try:
                if k:
                    envs._kr_set(name, k)
                else:
                    envs._kr_del(name)
            except Exception as e:
                raise apiserver.ApiError(
                    f"cannot reach the OS keyring: {e}")
            if apiserver.is_running():
                st = apiserver.status()
                apiserver.stop()
                apiserver.start(st["interface"], st["port"],
                                self._api_providers, k)
            got = {**apiserver.status(), "hasKey": bool(k)}
            self._bus.push({"type": "apisrv", **got})
            return {"api": got}
        return _api_call(do)()

    def api_server_config_set(self, interface, port):
        """Persist interface + port into loom.yaml's `api:` section (the
        on/off toggle is never persisted). A running API restarts on the
        new address."""
        def do():
            import yaml as _yaml
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            try:
                new_text = apiserver.inject_api_config(
                    p.read_text(encoding="utf-8"),
                    str(interface or ""), int(port))
            except (apiserver.ApiError, TypeError, ValueError) as e:
                raise libconfig.ConfigError(str(e))
            try:
                libconfig._api((_yaml.safe_load(new_text) or {}).get("api"))
            except _yaml.YAMLError as e:
                raise libconfig.ConfigError(
                    f"the result would not parse - nothing was written: {e}")
            library.write_file(root, p.name, new_text)
            self._bus.push({"type": "config", "config": self._config_or_error()})
            if apiserver.is_running():
                apiserver.stop()
                apiserver.start(str(interface or ""), int(port),
                                self._api_providers, self._api_key())
                self._bus.push({"type": "apisrv", **apiserver.status()})
            return {"api": {**apiserver.status(),
                            "cfg": {"interface": str(interface or ""),
                                    "port": int(port)},
                            "hasKey": bool(self._api_key())}}
        return _api_call(do)()

    # ---------------- quit gate ----------------
    def quit_blockers(self):
        return _api_call(lambda: {"chats": len(chat.running_chats()),
                                  "terminals": terminals.live_count()})()

    def quit_confirmed(self):
        """The user confirmed the quit dialog - the process MUST terminate.
        Normal teardown gets a few seconds; then the watchdog hard-exits so
        a wedged loop or non-daemon bridge thread can't leave a zombie in
        the terminal."""
        def work():
            _quit_state["quitting"] = True
            save_geometry()
            close_child_windows(self._bus)
            for cid in chat.running_chats():
                chat.stop(cid)
            terminals.shutdown()
            try:
                self._window.destroy()
            except Exception:
                pass
            time.sleep(4)
            _force_exit(0, grace=1.5)   # only reached if teardown wedged
        threading.Thread(target=work, daemon=True, name="quit-confirmed").start()
        return {"ok": True}


def on_window_closing(window, bus: Bus) -> bool:
    """The window's X hides to the system tray - chats keep running.
    Real quits (tray Quit, signals, no tray available) go through
    attempt_quit's blocker gate instead. Either way, child windows
    (popped-out diagnostics) close WITH the main window - a floating
    orphan with no way back would be worse than reopening it."""
    save_geometry()   # hide-to-tray included: the events stop firing
    if _quit_state["quitting"]:
        close_child_windows(bus)
        return True
    close_child_windows(bus)
    if _tray_state["available"]:
        try:
            window.hide()
        except Exception:
            return True
        if not _tray_state["notified"]:
            _tray_state["notified"] = True
            _tray_notify(
                "Loom",
                "Still running in the tray - servers stay up. Click "
                "the icon to reopen, or Quit to exit.")
        return False
    # no tray: closing the window is a real quit and takes the gate
    return attempt_quit(bus, window, closing_main=True)


def _parse_cli():
    import argparse
    ap = argparse.ArgumentParser(
        prog="loom",
        description="Loom - a library-centric local LLM workbench "
                    "(llama-server manager + chat). Fully offline; servers "
                    "bind unix sockets only. Launched from a terminal the "
                    "app DETACHES (logs to ~/.loom/loom.log) so closing the "
                    "terminal never kills it.")
    ap.add_argument("--debug", action="store_true",
                    help="open local DevTools on launch (implies --foreground)")
    ap.add_argument("--foreground", action="store_true",
                    help="stay attached to the terminal instead of detaching")
    ap.add_argument("--system-frame", action="store_true",
                    help="use the OS window frame instead of the integrated "
                         "titlebar (escape hatch for compositor quirks)")
    return ap.parse_args()


# --------------------------------------------------------------------------
# process lifecycle: single instance, terminal detach

def _pidfile() -> Path:
    return store.home() / "loom.pid"


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = existence probe, nothing delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _single_instance_gate() -> None:
    """Exit if another loom is alive (probably sitting in the tray); clean
    a dead instance's PID file. Runs BEFORE detaching so the message lands
    in the caller's terminal."""
    try:
        other = int(_pidfile().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if other != os.getpid() and _pid_running(other):
        print(f"loom is already running (pid {other}) - check the system "
              "tray. Exiting.", file=sys.stderr)
        raise SystemExit(1)
    _pidfile().unlink(missing_ok=True)


def _cleanup_pidfile() -> None:
    try:
        if _pidfile().read_text(encoding="utf-8").strip() == str(os.getpid()):
            _pidfile().unlink()
    except OSError:
        pass


def _write_pidfile() -> None:
    _pidfile().write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_cleanup_pidfile)


def _detach_from_terminal() -> None:
    """Fork away from the controlling terminal: the shell prompt returns,
    and closing the terminal never delivers a fatal HUP to Loom. MUST run
    before any thread or Qt object exists - fork does not carry threads."""
    if not hasattr(os, "fork"):
        return
    try:
        tty = any(f is not None and f.isatty()
                  for f in (sys.stdin, sys.stdout, sys.stderr))
    except (OSError, ValueError):
        tty = False
    if not tty:
        return
    log = store.home() / "loom.log"
    pid = os.fork()
    if pid > 0:
        print(f"loom detached from this terminal (pid {pid}) - logs: {log}\n"
              "use --foreground (or --debug) to stay attached")
        os._exit(0)
    os.setsid()   # own session: no controlling tty, immune to the shell's HUP
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(null)
    os.close(fd)
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (OSError, ValueError, AttributeError):
        pass


def main():
    args = _parse_cli()   # --help prints and exits here, no app execution
    store.ensure_dirs()
    _single_instance_gate()
    if not (args.foreground or args.debug):
        _detach_from_terminal()
    _write_pidfile()      # AFTER any fork so the PID is final
    compose.compose()   # render templates → frontend/index.html (~1 ms)

    atexit.register(mcp.shutdown)   # child processes must not outlive us

    bus = Bus()
    providers.on_status(lambda snap: bus.push({"type": "providers", **snap}))

    api = JsApi(bus)
    # every provider request pulls its API key (if any) from the keyring
    providers.set_key_resolver(api._provider_key)
    # however the process ends, our library claim must not outlive it
    atexit.register(lambda: library.release_lock(api._root))

    # opt into pywebview's native drop-path recording: its qt backend
    # only captures dropped files' REAL paths when at least one DOM drop
    # listener exists - we consume the recorded paths via drop_paths()
    try:
        from webview.dom import _dnd_state
        _dnd_state["num_listeners"] += 1
    except Exception:
        pass   # private API; folder drops degrade to a helpful toast

    # reopen where (and how big) the app was last closed
    geo = sane_geometry(store.window_geometry())
    _geom.update(geo)

    frameless = not args.system_frame
    window = webview.create_window(
        "Loom",
        url=FRONTEND_INDEX.as_uri(),   # real file:// URI, no web server
        js_api=api,
        width=geo.get("width", 1280), height=geo.get("height", 820),
        x=geo.get("x"), y=geo.get("y"),
        min_size=(940, 600),
        # integrated titlebar: the page draws it; drag/resize go through
        # startSystemMove/startSystemResize (compositor-native). easy_drag
        # stays OFF - it would fight text selection and terminals.
        frameless=frameless,
        easy_drag=False,
        # translucent window: the page paints its own ROUNDED background,
        # and the corners show through (needs a compositor - universal on
        # modern desktops; --system-frame avoids all of this)
        transparent=frameless,
    )
    api._frameless = frameless
    api.set_window(window)
    bus.set_window(window)

    window.events.loaded += lambda: harden_webengine(window)
    window.events.closing += lambda: on_window_closing(window, bus)
    _watch_geometry(window, bus)
    if geo.get("maximized"):
        def restore_max():
            _qt_maximize(window)
            bus.push({"type": "winstate", "maximized": True})
        window.events.loaded += restore_max

    # window icon + system tray (created on the Qt main thread once loaded)
    def install_chrome():
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        QTimer.singleShot(0, QApplication.instance(),
                          lambda: setup_tray(window, api, bus))
    window.events.loaded += install_chrome

    # Ctrl+C / SIGTERM: one signal surfaces the quit confirmation (never
    # die for a single stray signal); a second within 3s is the user
    # spamming - obey immediately. Delivery latency is bounded by the
    # signal pump timer below (~300ms).
    import signal as _signal

    def _sig_notify():
        print("loom: interrupt received - confirm the quit in the window, "
              "or press Ctrl+C again within 3s to force quit",
              file=sys.stderr, flush=True)
        _qt_focus(window)   # even from the tray: the window opens with
        bus.push({"type": "confirm_quit",   # the confirmation dialog up
                  "chats": len(chat.running_chats()),
                  "terminals": terminals.live_count()})

    def _sig_force():
        print("loom: repeated interrupt - terminating now",
              file=sys.stderr, flush=True)
        _quit_state["quitting"] = True
        _force_exit(130, grace=1.0)

    handler = make_signal_handler(_sig_notify, _sig_force)
    _signal.signal(_signal.SIGINT, handler)
    _signal.signal(_signal.SIGTERM, handler)

    # Pin Python's CYCLIC gc to the Qt main thread - a Qt wrapper freed by
    # gc on a worker thread destroys its C++ widget there, which is an
    # instant abort on Wayland.
    import gc
    gc.disable()

    def install_pumps():
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication

        def start():
            app_inst = QApplication.instance()
            g = QTimer(app_inst)
            g.setInterval(10_000)
            g.timeout.connect(lambda: gc.collect())
            g.start()
            # signal pump: keeps Ctrl+C delivered while Qt's loop spins
            s = QTimer(app_inst)
            s.setInterval(300)
            s.timeout.connect(lambda: None)
            s.start()
            _quit_state["_pumps"] = (g, s)
        QTimer.singleShot(0, QApplication.instance(), start)
    window.events.loaded += install_pumps

    if args.debug:
        window.events.loaded += lambda: open_local_devtools(window)

    code = 0
    try:
        webview.start(gui="qt")
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        # os._exit below SKIPS atexit handlers - every cleanup that must
        # happen on a normal quit has to run right here, explicitly
        save_geometry()
        terminals.shutdown()
        mcp.shutdown()   # MCP children are session leaders - reap them
        try:
            library.release_lock(api._root)
        except Exception:
            pass
        _cleanup_pidfile()
        # the window is gone and cleanup ran - nothing (non-daemon bridge
        # threads included) may keep the process alive in the terminal
        os._exit(code)


if __name__ == "__main__":
    main()
