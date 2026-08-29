"""In-app passphrase / host-key prompts for ssh.

Problem: reaching a remote host over ssh with an encrypted private key (or
a first-contact host-key confirmation) prompts on a TTY we don't have.
Solution:

  1. We write a tiny executable helper to ~/.loom/bin/loom-askpass.
  2. ssh runs with SSH_ASKPASS pointing at it.
  3. When ssh needs a secret, the helper connects to our unix socket,
     sends the prompt text, and blocks.
  4. The app raises a `prompt` event to the frontend; the user answers in a
     modal; the answer travels back through the socket to the helper's
     stdout, and ssh continues.

Session cache: answers the user marks "remember" are kept in-memory (never
on disk) keyed by prompt text, so one passphrase entry covers a burst of
ssh channels and later operations this session.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
import uuid
from pathlib import Path

from loom import store

_HELPER = """#!/usr/bin/env python3
import json, os, socket, sys

prompt = sys.argv[1] if len(sys.argv) > 1 else "Secret:"
sock_path = os.environ.get("LOOM_ASKPASS_SOCK", "")
if not sock_path:
    sys.exit(1)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(180)
s.connect(sock_path)
s.sendall(json.dumps({"prompt": prompt}).encode() + b"\\n")
buf = b""
while not buf.endswith(b"\\n"):
    chunk = s.recv(4096)
    if not chunk:
        sys.exit(1)
    buf += chunk
reply = json.loads(buf.decode())
if not reply.get("ok"):
    sys.exit(1)
sys.stdout.write(reply.get("secret", ""))
"""


def helper_path() -> Path:
    return store.bin_dir() / "loom-askpass"


def socket_path() -> Path:
    return store.run_dir() / f"askpass-{os.getpid()}.sock"


def install_helper() -> None:
    store.ensure_dirs()
    p = helper_path()
    p.write_text(_HELPER, encoding="utf-8")
    p.chmod(0o755)


class PromptBroker:
    """Bridges helper connections to UI answers."""

    def __init__(self, notify_ui):
        # notify_ui(prompt_id, prompt_text) -> pushes an event to the frontend
        self._notify_ui = notify_ui
        self._pending: dict[str, dict] = {}
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingUnixStreamServer | None = None

    # ---- called from the socket server thread (helper waiting) ----
    def request(self, prompt_text: str, timeout: float = 170.0) -> str | None:
        with self._lock:
            if prompt_text in self._cache:
                return self._cache[prompt_text]
        pid = uuid.uuid4().hex[:12]
        ev = threading.Event()
        rec = {"event": ev, "secret": None, "remember": False, "prompt": prompt_text}
        with self._lock:
            self._pending[pid] = rec
        self._notify_ui(pid, prompt_text)
        ok = ev.wait(timeout)
        with self._lock:
            self._pending.pop(pid, None)
            if ok and rec["secret"] is not None and rec["remember"]:
                self._cache[prompt_text] = rec["secret"]
        return rec["secret"] if ok else None

    # ---- called from the JsApi thread (user answered the modal) ----
    def answer(self, prompt_id: str, secret: str | None, remember: bool) -> bool:
        with self._lock:
            rec = self._pending.get(prompt_id)
            if rec is None:
                return False
            # mutations under the lock: request() re-reads this record under
            # the same lock after the event fires
            rec["secret"] = secret  # None = user cancelled
            rec["remember"] = bool(remember)
            rec["event"].set()
        return True

    def forget_cached(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---- socket server ----
    def start(self) -> None:
        install_helper()
        sp = socket_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        if sp.exists():
            sp.unlink()
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    line = self.rfile.readline(65536)
                    req = json.loads(line.decode("utf-8", "replace"))
                    secret = broker.request(str(req.get("prompt", "Secret:")))
                    if secret is None:
                        self.wfile.write(json.dumps({"ok": False}).encode() + b"\n")
                    else:
                        self.wfile.write(json.dumps({"ok": True, "secret": secret}).encode() + b"\n")
                except Exception:
                    try:
                        self.wfile.write(json.dumps({"ok": False}).encode() + b"\n")
                    except Exception:
                        pass

        self._server = socketserver.ThreadingUnixStreamServer(str(sp), Handler)
        self._server.daemon_threads = True
        os.chmod(sp, 0o600)
        t = threading.Thread(target=self._server.serve_forever, daemon=True, name="loom-askpass")
        t.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            try:
                socket_path().unlink(missing_ok=True)
            except OSError:
                pass
