"""Loom — the pywebview shell and the entire JS bridge surface.

Conventions (inherited from the pywebview knowledge base / the loom
incubator in CodeTree):
  * the frontend loads via a real ``file://`` URI — no web listener of any
    kind for the UI,
  * Ctrl/Cmd+Shift+I opens fully-local Chromium DevTools (a sibling Qt
    window via setDevToolsPage — never the appspot remote-debug redirect);
    ``--debug`` auto-opens them on load,
  * every JsApi method returns {"ok": True, ...} or {"ok": False, "error"},
  * long-running work (server starts, chat streaming) runs on daemon
    worker threads and reports through Bus.push → evaluate_js — NEVER on a
    pywebview bridge thread (those are non-daemon and would block
    interpreter shutdown),
  * managed llama-servers die WITH the app (srv.py lifelines).
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

from loom import (askpass, chat, chats, compose, containers, envs, libconfig,
                  library, models, search, srv, store, terminals)
from loom.compose import OUTPUT as FRONTEND_INDEX

DEFAULT_DEVTOOLS_PANEL = "console"


def harden_webengine(window):
    """pywebview's qt backend answers feature-permission requests with a raw
    int where PySide6 needs the enum — the JS promise hangs forever. Grant
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
    """Ctrl+Shift+I — Chromium's bundled DevTools in a sibling Qt window."""
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
# icons — drawn at runtime; the weave glyph matches the in-app logo

def _base_pixmap(size=64):
    """Rounded zinc tile with a woven warp/weft glyph (white on near-black
    — matches the app theme and reads on any tray background)."""
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
    """Tray tooltip = 'Loom - <library folder>' — how the user tells
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
    closing handler runs on a worker thread — raw Qt calls there are UB).
    On KDE the tray is a StatusNotifierItem, and Qt's showMessage sets the
    item's ATTENTION icon to the message icon (default: the blue
    dialog-information "i") and flips the status to NeedsAttention —
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
    window close, signals). Blocked by running servers or streaming chats:
    the window is brought up WITH the confirmation dialog showing.
    closing_main: called from the window's own closing handler — pywebview
    finishes the close itself, we must not destroy() recursively."""
    blockers = srv.running_count() + len(chat.running_chats()) \
        + terminals.live_count()
    if not force and blockers:
        _qt_focus(window)   # the decision surfaces in the main window
        bus.push({"type": "confirm_quit",
                  "servers": srv.running_count(),
                  "chats": len(chat.running_chats()),
                  "terminals": terminals.live_count()})
        return False
    _quit_state["quitting"] = True
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
    aboutToShow (Qt main thread — safe to read live state)."""
    from qtpy.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    app = QApplication.instance()
    if app is None:
        return
    try:
        # the StatusNotifierItem Id on KDE comes from desktopFileName and
        # falls back to the binary name — "python3" for us, which every
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
    # the tooltip names the open library — with several Looms on several
    # libraries, this is how the user tells the tray icons apart
    tray.setToolTip("Loom - " + api._root.name if api._root else "Loom")
    menu = QMenu()

    def do_show():
        _qt_focus(window)

    def stop_all():
        def work():
            for rec in srv.snapshot():
                if rec.get("state") in ("running", "loading", "starting"):
                    try:
                        srv.stop(str(rec.get("host") or ""), str(rec["id"]))
                    except Exception:
                        pass
        threading.Thread(target=work, daemon=True, name="tray-stopall").start()

    def rebuild():
        menu.clear()
        menu.addAction("Show Loom").triggered.connect(do_show)
        menu.addSeparator()
        models = []
        if api._root is not None:
            try:
                models = libconfig.load(api._root).get("models") or []
            except Exception:
                models = []
        states = {r.get("id"): r for r in srv.snapshot()}
        for m in models:
            state = (states.get(m["id"]) or {}).get("state") or "stopped"
            mark = {"running": "●", "loading": "◐", "starting": "◐",
                    "stopping": "◌", "error": "▲"}.get(state, "○")
            sub = menu.addMenu(f"{mark} {m['name']}")
            if state in ("running", "loading", "starting"):
                sub.addAction("Stop").triggered.connect(
                    lambda _=False, s=m["id"]: api.server_stop(s))
                sub.addAction("Restart").triggered.connect(
                    lambda _=False, s=m["id"]: api.server_restart(s))
            else:
                sub.addAction("Start").triggered.connect(
                    lambda _=False, s=m["id"]: api.server_start(s))
        if not models:
            menu.addAction("(no library open)").setEnabled(False)
        if srv.running_count():
            menu.addAction(f"Stop all servers ({srv.running_count()})"
                           ).triggered.connect(lambda _=False: stop_all())
        menu.addSeparator()
        # quit runs on a WORKER thread: the gate may surface a confirm in
        # the window — never block the Qt main loop
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
    """Pushes events into the page: LM_onEvent({...}). One dedicated daemon
    sender thread — evaluate_js blocks until Qt's main loop runs it, so
    sending from the main thread would deadlock and a bridge-thread send
    could hang process exit."""

    def __init__(self):
        self._window = None
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

    def push(self, event: dict) -> None:
        with self._lock:
            win = self._window
            if win is None:
                self._backlog.append(event)
                return
        self._sendq.put((win, f"window.LM_onEvent && LM_onEvent({json.dumps(event)})"))


def _api_call(fn):
    """Exceptions become {ok:false,error} — the bridge never dies."""
    def inner(*a, **kw):
        try:
            out = fn(*a, **kw)
            if isinstance(out, dict) and "ok" in out:
                return out
            return {"ok": True, "data": out}
        except (srv.SrvError, library.LibraryError, libconfig.ConfigError,
                chats.ChatError, containers.ContainerError,
                terminals.TermError, models.ModelsError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return inner


_quit_state = {"quitting": False}


def uuid_job() -> str:
    import uuid
    return uuid.uuid4().hex[:10]


def _force_exit(code: int = 0, grace: float = 2.0) -> None:
    """Terminate the PROCESS. srv.shutdown cuts the server lifelines with a
    TERM-first grace; os._exit then ends the interpreter unconditionally —
    non-daemon pywebview bridge threads must never hold a corpse alive.
    (Even on the _exit itself, the kernel closing our pipe ends kills any
    remaining supervised server.)"""
    try:
        srv.shutdown(grace)
    except Exception:
        pass
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

    def __init__(self, bus: Bus, broker: askpass.PromptBroker):
        self._bus = bus
        self._broker = broker
        self._window = None
        self._root: Path | None = None   # the open library

    def set_window(self, window):
        self._window = window

    def _toast(self, level: str, msg: str):
        self._bus.push({"type": "toast", "level": level, "msg": srv.tilde(msg)})

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
        """The custom titlebar's ✕ — same semantics as a native close:
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
        # converge server states for this library's models in the background
        if isinstance(cfg, dict) and not cfg.get("error"):
            models = cfg.get("models") or []
            threading.Thread(target=lambda: srv.refresh(models),
                             daemon=True, name="srv-refresh").start()
        chats.purge_empty_archived(root)   # empty chats are never worth keeping
        open_chats = [c for c in chats.list_chats(root) if not c["archived"]]
        sess = store.session_get(str(root))
        return {"library": str(root), "config": cfg, "openChats": open_chats,
                "session": sess,
                # no saved session = the first visit — land on the docs
                "firstOpen": sess is None}

    def _config_or_error(self):
        try:
            return libconfig.load(self._need_root())
        except libconfig.ConfigError as e:
            return {"error": str(e)}

    def session_save(self, session):
        """The frontend pushes its UI session (tabs, active tab, open file,
        tree expansion, panel width) whenever it changes — reopening the
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

    # ---------------- servers ----------------
    def servers_get(self):
        def do():
            cfg = self._config_or_error()
            states = {r.get("id"): r for r in srv.snapshot()}
            models = []
            for m in (cfg.get("models") or []) if not cfg.get("error") else []:
                row = dict(m)
                st = states.get(m["id"]) or {}
                row["state"] = st.get("state") or "stopped"
                row["detail"] = st.get("detail") or ""
                row["nCtx"] = st.get("nCtx")
                try:
                    row["command"] = "llama-server --host <socket> " + " ".join(
                        srv.compose_args(m))
                except srv.SrvError as e:
                    row["command"] = ""
                    row["configError"] = str(e)
                models.append(row)
            return {"models": models, "config": cfg}
        return _api_call(do)()

    def _model_rec(self, mid: str) -> dict:
        cfg = libconfig.load(self._need_root())
        rec = next((m for m in cfg["models"] if m["id"] == mid), None)
        if rec is None:
            raise srv.SrvError("no such model in loom.yaml")
        return rec

    def server_start(self, mid):
        def do():
            rec = self._model_rec(str(mid))
            if srv.state_of(rec["id"]) in ("starting", "loading"):
                return {"already": True}

            def work():
                try:
                    srv.start(rec, notice=lambda m: self._bus.push(
                        {"type": "note", "id": rec["id"], "msg": m}))
                except srv.SrvError as e:
                    self._toast("err", str(e))
                except Exception as e:
                    traceback.print_exc()
                    self._toast("err", f"start: {e}")
            threading.Thread(target=work, daemon=True,
                             name=f"start-{mid}").start()
            return {}
        return _api_call(do)()

    def server_stop(self, mid):
        def do():
            rec = self._model_rec(str(mid))

            def work():
                try:
                    srv.stop(rec["host"], rec["id"])
                except srv.SrvError as e:
                    self._toast("err", str(e))
            threading.Thread(target=work, daemon=True,
                             name=f"stop-{mid}").start()
            return {}
        return _api_call(do)()

    def server_restart(self, mid):
        def do():
            rec = self._model_rec(str(mid))

            def work():
                try:
                    srv.stop(rec["host"], rec["id"])
                    srv.start(rec, notice=lambda m: self._bus.push(
                        {"type": "note", "id": rec["id"], "msg": m}))
                except srv.SrvError as e:
                    self._toast("err", str(e))
                except Exception as e:
                    traceback.print_exc()
                    self._toast("err", f"restart: {e}")
            threading.Thread(target=work, daemon=True,
                             name=f"restart-{mid}").start()
            return {}
        return _api_call(do)()

    def server_log(self, mid, nbytes=16000):
        def do():
            rec = self._model_rec(str(mid))
            return {"log": srv.log_tail(rec["host"], rec["id"],
                                        int(nbytes or 16000))}
        return _api_call(do)()

    def server_log_clear(self, mid):
        def do():
            rec = self._model_rec(str(mid))
            srv.log_clear(rec["host"], rec["id"])
            return {}
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
        """New chat. `template` (from Ctrl+N on an open chat) carries the
        source chat's model, permission mode, and folder attachments — a
        clean context window with the same working setup."""
        def do():
            cfg = self._config_or_error()
            model = ""
            if not cfg.get("error"):
                model = (cfg.get("chat") or {}).get("model") or ""
                if not model and cfg.get("models"):
                    model = cfg["models"][0]["name"]
            t = template if isinstance(template, dict) else {}
            if str(t.get("model") or "").strip():
                model = str(t["model"]).strip()
            c = chats.new_chat(self._need_root(), model)
            pm = str(t.get("permMode") or "").strip()
            if pm and (cfg.get("error") or pm in (cfg.get("permissionModes") or {})):
                c["permMode"] = pm
            folders = self._clean_folders(t.get("folders"))
            if folders:
                c["folders"] = folders
            if t.get("network"):
                c["network"] = True
            if pm or folders or t.get("network"):
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

    def chat_get(self, chat_id, tail=None):
        """The chat, optionally windowed to the last `tail` messages —
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
                    dur = int((t.get("prompt_ms") or 0)
                              + (t.get("predicted_ms") or 0)) or delta
                    calls = m.get("tool_calls") or []
                    think_tok = est(m.get("thinking")) if m.get("thinking") else 0
                    body_tok = est(m.get("content")) \
                        + sum(est(json.dumps(tc)) for tc in calls)
                    total_tok = max(1, think_tok + body_tok)
                    if m.get("thinking"):
                        # one generation produced both — split its time
                        # proportionally to the tokens each part got
                        rows.append({"i": i, "kind": "think", "ts": ts,
                                     "durMs": dur * think_tok // total_tok,
                                     "tokens": think_tok,
                                     "preview": prev(m.get("thinking")),
                                     "extra": ""})
                    rows.append({"i": i, "kind": "assistant", "ts": ts,
                                 "durMs": dur - (dur * think_tok // total_tok),
                                 "tokens": body_tok,
                                 "preview": prev(m.get("content"))
                                 or ("→ " + ", ".join(
                                     tc.get("function", {}).get("name", "?")
                                     for tc in calls)),
                                 "extra": (str(len(calls)) + " tool call(s)")
                                 if calls else ""})
                elif role == "tool":
                    rows.append({"i": i, "kind": "tool", "ts": ts,
                                 "durMs": delta,
                                 "tokens": est(m.get("content")),
                                 "preview": prev(m.get("content")),
                                 "extra": (m.get("name") or "tool")
                                 + ("" if m.get("ok", True) else " · FAILED")})
                elif role == "compact":
                    rows.append({"i": i, "kind": "compact", "ts": ts,
                                 "durMs": delta,
                                 "tokens": est(m.get("content")),
                                 "preview": prev(m.get("content")),
                                 "extra": str(m.get("replaced") or 0)
                                 + " messages summarized"})
            nctx = 0
            try:
                nctx = chat._nctx_for(libconfig.load(root), c)
            except libconfig.ConfigError:
                pass
            return {"rows": rows, "title": c.get("title") or "Chat",
                    "nCtx": nctx}
        return _api_call(do)()

    def chat_compact(self, chat_id):
        """Manual 'Compact now' — same machinery as auto-compaction."""
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
        An EMPTY chat (no messages) is deleted instead — never archived."""
        def do():
            cid = str(chat_id)
            if chat.is_running(cid):
                if not force:
                    return {"ok": False, "error": "inference is running",
                            "needsConfirm": True}
                chat.stop(cid)
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
            chats.delete_chat(self._need_root(), cid)
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

    def chat_set_model(self, chat_id, model):
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
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

    def chat_set_network(self, chat_id, enabled):
        """Per-chat container network access (off by default). A USER
        decision only — nothing in a library can flip this."""
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            c["network"] = bool(enabled)
            chats.save_chat(self._need_root(), c)
            return {"network": c["network"]}
        return _api_call(do)()

    def chat_set_folders(self, chat_id, folders):
        def do():
            c = chats.load_chat(self._need_root(), str(chat_id))
            clean = self._clean_folders(folders)
            c["folders"] = clean
            chats.save_chat(self._need_root(), c)
            return {"folders": clean}
        return _api_call(do)()

    def drop_paths(self):
        """Native filesystem paths of the OS drag-drop that just landed.
        The qt backend records them (we bump its listener count at boot —
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
        """dir / file / missing — lets the composer's drop handler tell a
        FOLDER drop (attach as a mount) from an image drop."""
        def do():
            p = Path(str(path or "")).expanduser()
            kind = "dir" if p.is_dir() else "file" if p.is_file() else "missing"
            return {"kind": kind}
        return _api_call(do)()

    def git_info(self, paths):
        """{path: branch} for the attached folders that are git worktrees —
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
        The path persists — chats re-encode it on every send."""
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
            # a stream the user just cancelled may still be unwinding —
            # wait that out instead of bouncing the send
            if chat.is_running(cid) and not chat.wait_if_cancelling(cid):
                raise chats.ChatError("a response is already streaming")
            c = chats.load_chat(root, cid)
            msg = {"role": "user", "content": str(text or ""),
                   "ts": __import__("time").time_ns() // 1_000_000}
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
        """Copy the user's attachments into the chat's artifact folder
        (/artifacts/uploads in the container) so shell/file tools can
        reach them. Best-effort — a failed copy must not block the send."""
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

    def artifact_save(self, chat_id, name):
        """Save one artifact where the user chooses: files copy, folders
        zip. Backed by the /artifacts pills in the chat's attach bar."""
        def do():
            import shutil as _sh
            root = self._need_root()
            base = chats.artifacts_dir(root, str(chat_id))
            src = (base / str(name)).resolve()
            if src != base and base not in src.parents:
                raise chats.ChatError(f"bad artifact name: {name}")
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
        """pref = {method, level} — or None/{} to restore the default
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
        """Run the loop on the chat AS IS — no new user message. Backs the
        Retry button (last message is the user's) and the Continue button
        (the model stopped abruptly)."""
        def do():
            cid = str(chat_id)
            if chat.is_running(cid) and not chat.wait_if_cancelling(cid):
                raise chats.ChatError("a response is already streaming")
            c = chats.load_chat(self._need_root(), cid)
            if not c.get("messages"):
                raise chats.ChatError("nothing to continue yet")
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

    def servers_refresh(self):
        """Re-probe every configured model's host and push fresh states."""
        def do():
            cfg = self._config_or_error()
            models = cfg.get("models") or [] if not cfg.get("error") else []
            threading.Thread(target=lambda: srv.refresh(models),
                             daemon=True, name="srv-refresh-ui").start()
            return {}
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
                    bool(o.get("network")),
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

    # ---------------- the models utility ----------------
    def models_hosts(self):
        return _api_call(lambda: {"hosts": models.hosts()})()

    def models_host_add(self, host):
        return _api_call(lambda: {"hosts": models.add_host(str(host))})()

    def models_host_remove(self, host):
        return _api_call(lambda: {"hosts": models.remove_host(str(host))})()

    def models_hosts_reorder(self, order):
        return _api_call(lambda: {"hosts": models.reorder_hosts(
            order if isinstance(order, list) else [])})()

    def models_scan(self, host=""):
        return _api_call(lambda: {"host": str(host or ""),
                                  "entries": models.scan(str(host or ""))})()

    def models_yaml(self, selection):
        return _api_call(lambda: {"yaml": models.yaml_snippet(
            selection if isinstance(selection, list) else [])})()

    def models_download(self, url, filename="", total=0):
        """`url` may be a list of shard urls (a split model)."""
        def do():
            job = uuid_job()
            models.start_download(
                self._bus.push, job,
                url if isinstance(url, list) else str(url),
                str(filename or ""), total)
            return {"job": job}
        return _api_call(do)()

    def models_downloads(self):
        return _api_call(lambda: {"downloads": models.downloads()})()

    def models_dl_pause(self, job):
        return _api_call(lambda: {"paused": models.pause_download(str(job))})()

    def models_dl_resume(self, job):
        def do():
            models.resume_download(self._bus.push, str(job))
            return {"resumed": True}
        return _api_call(do)()

    def models_dl_cancel(self, job):
        def do():
            models.cancel_download(str(job))
            return {"cancelled": True}
        return _api_call(do)()

    def models_dl_dismiss(self, job):
        def do():
            models.dismiss_download(str(job))
            return {"dismissed": True}
        return _api_call(do)()

    def models_dl_clear(self):
        def do():
            models.clear_downloads()
            return {"cleared": True}
        return _api_call(do)()

    def models_push(self, local_path, host):
        """`local_path` may be a list of shard paths (a split model)."""
        def do():
            job = uuid_job()
            models.start_push(
                self._bus.push, job,
                local_path if isinstance(local_path, list) else str(local_path),
                str(host))
            return {"job": job}
        return _api_call(do)()

    def models_cancel(self, job):
        return _api_call(lambda: {"cancelled": models.cancel(str(job))})()

    def models_repo(self, spec):
        return _api_call(lambda: {"files": models.repo_files(str(spec))})()

    def models_meta(self, path, host=""):
        return _api_call(lambda: {"meta": models.gguf_meta(
            str(path), str(host or ""))})()

    def config_add_model_text(self, snippet):
        """Inject the USER-EDITED snippet from the wizard's final step —
        their exact lines land in loom.yaml (validated first), and the rest
        of the file stays byte-for-byte untouched."""
        def do():
            import yaml as _yaml
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            lines = models.snippet_entry_lines(str(snippet or ""))
            new_text = models.inject_lines(
                p.read_text(encoding="utf-8"), lines)
            try:
                raw = _yaml.safe_load(new_text) or {}
                libconfig._models(raw.get("models"))
            except _yaml.YAMLError as e:
                raise libconfig.ConfigError(
                    f"the result would not parse — nothing was written: {e}")
            library.write_file(root, p.name, new_text)
            self._bus.push({"type": "config", "config": self._config_or_error()})
            return {"configFile": p.name}
        return _api_call(do)()

    def config_add_model(self, entry):
        """The wizard's final step: inject one model entry into loom.yaml
        NON-destructively (comments and ordering untouched), validate the
        result parses, then write. A result that would not parse is never
        written."""
        def do():
            import yaml as _yaml
            root = self._need_root()
            p = library.config_path(root)
            if p is None:
                raise libconfig.ConfigError("the library has no loom.yaml")
            text = p.read_text(encoding="utf-8")
            new_text = models.inject_model(
                text, entry if isinstance(entry, dict) else {})
            try:
                raw = _yaml.safe_load(new_text) or {}
                libconfig._models(raw.get("models"))
            except _yaml.YAMLError as e:
                raise libconfig.ConfigError(
                    f"the injected yaml would not parse — nothing was "
                    f"written: {e}")
            library.write_file(root, p.name, new_text)
            self._bus.push({"type": "config", "config": self._config_or_error()})
            return {"configFile": p.name}
        return _api_call(do)()

    # ---------------- switching libraries ----------------
    def switch_blockers(self):
        """What switching away kills: the names of llama-servers that will
        be stopped and how many chats are mid-generation."""
        def do():
            servers = [str(r.get("name") or r.get("id"))
                       for r in srv.snapshot()
                       if r.get("state") in ("running", "loading", "starting")]
            return {"servers": servers, "chats": len(chat.running_chats()),
                    "terminals": terminals.live_count()}
        return _api_call(do)()

    def library_close(self):
        """Leave the current library: cancel streaming chats and stop every
        managed server — a library's servers must not outlive it invisibly
        (after a switch there would be no UI that can stop them)."""
        def do():
            library.release_lock(self._root)
            self._root = None
            _set_tray_tooltip("Loom")
            for cid in chat.running_chats():
                chat.stop(cid)
            # terminals run the library's containers — they close with it
            threading.Thread(target=terminals.shutdown, daemon=True,
                             name="lib-close-terms").start()
            stop_list = [(str(r.get("host") or ""), str(r.get("id")))
                         for r in srv.snapshot()
                         if r.get("state") in ("running", "loading", "starting")]

            def work():
                for host, sid in stop_list:
                    try:
                        srv.stop(host, sid)
                    except Exception:
                        pass
                    srv.forget(sid)
            if stop_list:
                threading.Thread(target=work, daemon=True,
                                 name="lib-close-stop").start()
            return {"stopping": len(stop_list)}
        return _api_call(do)()

    # ---------------- quit gate ----------------
    def quit_blockers(self):
        return _api_call(lambda: {"servers": srv.running_count(),
                                  "chats": len(chat.running_chats()),
                                  "terminals": terminals.live_count()})()

    def quit_confirmed(self):
        """The user confirmed the quit dialog — the process MUST terminate.
        Normal teardown gets a few seconds; then the watchdog hard-exits so
        a wedged loop or non-daemon bridge thread can't leave a zombie in
        the terminal."""
        def work():
            _quit_state["quitting"] = True
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

    # ---------------- secret prompts (ssh) ----------------
    def prompt_answer(self, prompt_id, secret, remember):
        return {"ok": self._broker.answer(prompt_id, secret, bool(remember))}

    def prompt_cancel(self, prompt_id):
        return {"ok": self._broker.answer(prompt_id, None, False)}


def on_window_closing(window, bus: Bus) -> bool:
    """The window's X hides to the system tray — servers and chats keep
    running. Real quits (tray Quit, signals, no tray available) go through
    attempt_quit's blocker gate instead."""
    if _quit_state["quitting"]:
        return True
    if _tray_state["available"]:
        try:
            window.hide()
        except Exception:
            return True
        if not _tray_state["notified"]:
            _tray_state["notified"] = True
            _tray_notify(
                "Loom",
                "Still running in the tray — servers stay up. Click "
                "the icon to reopen, or Quit to exit.")
        return False
    # no tray: closing the window is a real quit and takes the gate
    return attempt_quit(bus, window, closing_main=True)


def _parse_cli():
    import argparse
    ap = argparse.ArgumentParser(
        prog="loom",
        description="Loom — a library-centric local LLM workbench "
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
        print(f"loom is already running (pid {other}) — check the system "
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
    before any thread or Qt object exists — fork does not carry threads."""
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
        print(f"loom detached from this terminal (pid {pid}) — logs: {log}\n"
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

    atexit.register(srv.shutdown)

    bus = Bus()
    srv.on_status(lambda snap: bus.push({"type": "srv", **snap}))
    broker = askpass.PromptBroker(
        lambda pid, text: bus.push({"type": "prompt", "id": pid, "text": text}))
    broker.start()

    api = JsApi(bus, broker)
    # however the process ends, our library claim must not outlive it
    atexit.register(lambda: library.release_lock(api._root))

    # opt into pywebview's native drop-path recording: its qt backend
    # only captures dropped files' REAL paths when at least one DOM drop
    # listener exists — we consume the recorded paths via drop_paths()
    try:
        from webview.dom import _dnd_state
        _dnd_state["num_listeners"] += 1
    except Exception:
        pass   # private API; folder drops degrade to a helpful toast

    frameless = not args.system_frame
    window = webview.create_window(
        "Loom",
        url=FRONTEND_INDEX.as_uri(),   # real file:// URI, no web server
        js_api=api,
        width=1280, height=820,
        min_size=(940, 600),
        # integrated titlebar: the page draws it; drag/resize go through
        # startSystemMove/startSystemResize (compositor-native). easy_drag
        # stays OFF — it would fight text selection and terminals.
        frameless=frameless,
        easy_drag=False,
        # translucent window: the page paints its own ROUNDED background,
        # and the corners show through (needs a compositor — universal on
        # modern desktops; --system-frame avoids all of this)
        transparent=frameless,
    )
    api._frameless = frameless
    api.set_window(window)
    bus.set_window(window)

    window.events.loaded += lambda: harden_webengine(window)
    window.events.closing += lambda: on_window_closing(window, bus)

    # window icon + system tray (created on the Qt main thread once loaded)
    def install_chrome():
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        QTimer.singleShot(0, QApplication.instance(),
                          lambda: setup_tray(window, api, bus))
    window.events.loaded += install_chrome

    # Ctrl+C / SIGTERM: one signal surfaces the quit confirmation (never
    # die for a single stray signal); a second within 3s is the user
    # spamming — obey immediately. Delivery latency is bounded by the
    # signal pump timer below (~300ms).
    import signal as _signal

    def _sig_notify():
        print("loom: interrupt received — confirm the quit in the window, "
              "or press Ctrl+C again within 3s to force quit",
              file=sys.stderr, flush=True)
        _qt_focus(window)   # even from the tray: the window opens with
        bus.push({"type": "confirm_quit",   # the confirmation dialog up
                  "servers": srv.running_count(),
                  "chats": len(chat.running_chats()),
                  "terminals": terminals.live_count()})

    def _sig_force():
        print("loom: repeated interrupt — terminating now",
              file=sys.stderr, flush=True)
        _quit_state["quitting"] = True
        _force_exit(130, grace=1.0)

    handler = make_signal_handler(_sig_notify, _sig_force)
    _signal.signal(_signal.SIGINT, handler)
    _signal.signal(_signal.SIGTERM, handler)

    # Pin Python's CYCLIC gc to the Qt main thread — a Qt wrapper freed by
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
        broker.stop()
        terminals.shutdown()
        srv.shutdown()   # nothing keeps running after the app exits
        # the window is gone and cleanup ran — nothing (non-daemon bridge
        # threads included) may keep the process alive in the terminal
        os._exit(code)


if __name__ == "__main__":
    main()
