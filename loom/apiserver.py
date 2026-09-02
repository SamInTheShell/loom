"""The OpenAI-compatible inference API — Loom's one opt-in TCP door.

Managed llama-servers bind unix sockets only; this module is the single
place a TCP port can open, and only while the user has the API toggled ON.
The toggle is deliberately NOT persisted — every Loom launch starts with
the API off. Interface + port live in loom.yaml (`api:`), edited through
the API Server tab (inject_api_config keeps the rest of the file
byte-for-byte untouched).

Routes (all under the configured interface:port):
    GET  /v1/models            the configured models, live state included
    POST /v1/chat/completions  proxied to the model named in the body
    POST /v1/completions       ─ " ─
    POST /v1/embeddings        ─ " ─ (the target llama-server must run
                               with --embeddings, or be an embedding
                               model — the flag rides in `flags:`)

Routing: the request's "model" field matches a loom.yaml model NAME (or
id). When it matches nothing and exactly ONE model is running, that model
serves — drop-in clients that send "gpt-4o-mini" just work. Only RUNNING
models serve; anything else is a 503 with a hint. The API never starts
servers itself: a stampede of API clients must not eject each other's
models through the per-host concurrency limit.

Responses stream: bytes are pumped from the llama-server socket to the
client as they arrive (SSE included), one connection per request.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loom import srv

DEFAULT_INTERFACE = "127.0.0.1"
DEFAULT_PORT = 1234
GEN_TIMEOUT = 3600   # a big model can chew a huge prompt for a long time

PROXY_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings")


class ApiError(Exception):
    pass


_lock = threading.Lock()
_state: dict = {"server": None, "interface": "", "port": 0}


def is_running() -> bool:
    with _lock:
        return _state["server"] is not None


def status() -> dict:
    with _lock:
        run = _state["server"] is not None
        return {"running": run,
                "interface": _state["interface"] if run else "",
                "port": _state["port"] if run else 0}


def start(interface: str, port: int, get_models) -> dict:
    """Bind and serve. `get_models` is called per request and must return
    the CURRENT loom.yaml model list (fresh — yaml edits apply live)."""
    iface = str(interface or DEFAULT_INTERFACE).strip() or DEFAULT_INTERFACE
    port = int(port or DEFAULT_PORT)
    with _lock:
        if _state["server"] is not None:
            return status()
        try:
            httpd = ThreadingHTTPServer((iface, port), _Handler)
        except OSError as e:
            raise ApiError(f"cannot bind {iface}:{port} — {e}")
        httpd.daemon_threads = True
        httpd.loom_models = get_models
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="loom-api").start()
        _state.update(server=httpd, interface=iface,
                      port=httpd.server_address[1])   # real port (0 = pick)
    return status()


def stop() -> dict:
    with _lock:
        httpd = _state["server"]
        _state.update(server=None, interface="", port=0)
    if httpd is not None:
        try:
            httpd.shutdown()
            httpd.server_close()
        except OSError:
            pass
    return status()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "loom-api"

    def log_message(self, *a):
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": {"message": msg, "type": "loom",
                                    "code": code}})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/v1/models", "/models"):
            data = [{"id": m["name"], "object": "model", "owned_by": "loom",
                     "state": srv.state_of(m["id"]),
                     "host": m.get("host") or "local"}
                    for m in self.server.loom_models()]
            self._json(200, {"object": "list", "data": data})
        elif path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._err(404, f"no such route: {path}")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in PROXY_PATHS:
            self._err(404, f"no such route: {path} — this API serves "
                      + ", ".join(PROXY_PATHS))
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            self._err(400, "the request body is not valid JSON")
            return
        models = self.server.loom_models()
        name = str((payload or {}).get("model") or "")
        rec = next((m for m in models
                    if m["name"] == name or m["id"] == name), None)
        if rec is None:
            # unknown name and exactly one model live → serve with it
            live = [m for m in models
                    if srv.state_of(m["id"]) == "running"]
            if len(live) == 1:
                rec = live[0]
        if rec is None:
            self._err(404, f"unknown model {name!r} — configured: "
                      + (", ".join(m["name"] for m in models) or "(none)"))
            return
        if srv.state_of(rec["id"]) != "running":
            self._err(503, f"{rec['name']} is not running — start it in "
                      "Loom first (this API never starts servers itself)")
            return
        sock = srv.sock_of(rec["id"])
        if not sock:
            self._err(503, f"{rec['name']} has no socket yet — try again")
            return
        try:
            resp = srv.http_to(rec.get("host") or "", sock, "POST", path,
                               body, {"Content-Type": "application/json"},
                               timeout=GEN_TIMEOUT)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type",
                             e.headers.get("Content-Type")
                             or "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except (urllib.error.URLError, OSError) as e:
            self._err(502, f"cannot reach {rec['name']}'s server: "
                      f"{getattr(e, 'reason', e)}")
            return
        # stream the answer through — SSE chunks land as they arrive
        with resp:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.headers.get("Content-Type")
                             or "application/json")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass   # the client hung up — llama-server sees the close


# ---------------------------------------------------------------------------
# loom.yaml `api:` section — written non-destructively, like the models
# block: only OUR section is replaced, everything else stays byte-for-byte

_API_KEY_RE = re.compile(r"^api:\s*(#.*)?$")


def inject_api_config(text: str, interface: str, port: int) -> str:
    """Set (or append) the `api:` section in loom.yaml TEXT."""
    iface = str(interface or DEFAULT_INTERFACE).strip() or DEFAULT_INTERFACE
    port = int(port)
    if not (1 <= port <= 65535):
        raise ApiError("the port must be 1-65535")
    new = ["api:", f"  interface: {iface}", f"  port: {port}"]
    src = text.splitlines()
    i = next((n for n, ln in enumerate(src) if _API_KEY_RE.match(ln)), None)
    if i is None:
        out = src[:]
        if out and out[-1].strip():
            out.append("")
        return "\n".join(out + new) + "\n"
    # consume the old block: indented lines, blanks and comments after the
    # key — then give back any trailing blank/comment padding, so the
    # spacing that separates the next section stays where it was
    j = i + 1
    while j < len(src) and (not src[j].strip() or src[j].startswith(" ")
                            or src[j].lstrip().startswith("#")):
        j += 1
    k = j
    while k > i + 1 and (not src[k - 1].strip()
                         or src[k - 1].lstrip().startswith("#")):
        k -= 1
    return "\n".join(src[:i] + new + src[k:]) + "\n"
