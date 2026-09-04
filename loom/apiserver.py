"""The OpenAI-compatible aggregation API - Loom's one opt-in TCP door.

This module is the single place a TCP port can open, and only while the
user has the API toggled ON. The toggle is deliberately NOT persisted -
every Loom launch starts with the API off. Interface + port live in
loom.yaml (`api:`), edited through the API Server tab (inject_api_config
keeps the rest of the file byte-for-byte untouched).

Routes (all under the configured interface:port):
    GET  /v1/models            every model of every configured provider
    POST /v1/chat/completions  proxied to the provider serving the model
    POST /v1/completions       ─ " ─
    POST /v1/embeddings        ─ " ─

Routing: the request's "model" field matches a model id from any
provider's cached list (or "provider/model" to disambiguate). When it
matches nothing and exactly ONE provider is reachable with exactly ONE
model, that serves - drop-in clients that send "gpt-4o-mini" just work.

AUTH: an optional API key. When set, every route except /health
requires `Authorization: Bearer <key>` or `x-api-key: <key>` (the same
contract llama-server and ninfer use, so the same client config works
against all three). The key itself lives in the OS keyring, never in
loom.yaml - the caller passes it to start().

Responses stream: bytes are pumped from the provider connection to the
client as they arrive (SSE included), one connection per request.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loom import providers

DEFAULT_INTERFACE = "127.0.0.1"
DEFAULT_PORT = 1234
GEN_TIMEOUT = 3600   # a big model can chew a huge prompt for a long time

PROXY_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings")


class ApiError(Exception):
    pass


_lock = threading.Lock()
_state: dict = {"server": None, "interface": "", "port": 0, "key": ""}


def is_running() -> bool:
    with _lock:
        return _state["server"] is not None


def status() -> dict:
    with _lock:
        run = _state["server"] is not None
        return {"running": run,
                "interface": _state["interface"] if run else "",
                "port": _state["port"] if run else 0,
                "keyed": run and bool(_state["key"])}


def start(interface: str, port: int, get_providers,
          api_key: str = "") -> dict:
    """Bind and serve. `get_providers` is called per request and must
    return the CURRENT loom.yaml provider list (fresh - yaml edits apply
    live). A non-empty `api_key` gates every route except /health."""
    iface = str(interface or DEFAULT_INTERFACE).strip() or DEFAULT_INTERFACE
    port = int(port or DEFAULT_PORT)
    with _lock:
        if _state["server"] is not None:
            return status()
        try:
            httpd = ThreadingHTTPServer((iface, port), _Handler)
        except OSError as e:
            raise ApiError(f"cannot bind {iface}:{port} - {e}")
        httpd.daemon_threads = True
        httpd.loom_providers = get_providers
        httpd.loom_api_key = str(api_key or "")
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="loom-api").start()
        _state.update(server=httpd, interface=iface,
                      port=httpd.server_address[1],   # real port (0 = pick)
                      key=str(api_key or ""))
    return status()


def stop() -> dict:
    with _lock:
        httpd = _state["server"]
        _state.update(server=None, interface="", port=0, key="")
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

    def _authed(self) -> bool:
        """True when no key is set, or the request carries it. Bearer
        and x-api-key both count (matches llama-server and ninfer);
        constant-time comparison."""
        key = str(getattr(self.server, "loom_api_key", "") or "")
        if not key:
            return True
        auth = str(self.headers.get("Authorization") or "")
        if auth.startswith("Bearer ") \
                and hmac.compare_digest(auth[7:], key):
            return True
        return hmac.compare_digest(
            str(self.headers.get("x-api-key") or ""), key)

    def _reject_unauthed(self) -> bool:
        """401 the request unless it is authed. /health stays public -
        reachability probes must not need the secret."""
        if self.path.split("?")[0] == "/health" or self._authed():
            return False
        self._err(401, "missing or invalid API key - send "
                  "'Authorization: Bearer <key>' or 'x-api-key: <key>'")
        return True

    @staticmethod
    def _probe_missing(provs) -> None:
        """Providers the registry has never seen get probed NOW - an API
        client must not 404 just because nobody opened the Providers tab
        this session."""
        for p in provs:
            if not providers.status_of(p["name"]):
                providers.probe(p)

    def _route(self, name: str):
        """(provider record, model id) for a requested model name.
        Accepts a bare model id (searched across providers, cached lists)
        or 'provider/model'. Unknown ids fall back so drop-in clients
        just work: one known model anywhere → serve it; every known
        model on ONE provider → pass the name through verbatim and let
        that server decide (single-model llama-servers ignore it)."""
        provs = self.server.loom_providers()
        by_name = {p["name"]: p for p in provs}
        if "/" in name:
            pname, mid = name.split("/", 1)
            if pname in by_name:
                return by_name[pname], mid
        self._probe_missing(provs)

        def scan():
            found = []
            for p in provs:
                for m in providers.models_of(p["name"]):
                    if m["id"] == name:
                        return (p, name), found
                    found.append((p, m["id"]))
            return None, found

        hit, candidates = scan()
        if hit is None and provs:
            # cache miss: the provider may serve a model the last probe
            # never saw - re-check STALE providers (throttled so drop-in
            # clients hammering an unknown alias don't probe per request)
            import time as _time
            now = int(_time.time() * 1000)
            reprobed = False
            for p in provs:
                if now - int(providers.status_of(p["name"]).get("ts") or 0) \
                        > 10_000:
                    providers.probe(p)
                    reprobed = True
            if reprobed:
                hit, candidates = scan()
        if hit:
            return hit
        if len(candidates) == 1:
            return candidates[0]
        if candidates and len({p["name"] for p, _m in candidates}) == 1:
            return candidates[0][0], name   # one provider - it decides
        return None, None

    def do_GET(self):
        if self._reject_unauthed():
            return
        path = self.path.split("?")[0]
        if path in ("/v1/models", "/models"):
            provs = self.server.loom_providers()
            self._probe_missing(provs)
            data = []
            for p in provs:
                st = providers.status_of(p["name"])
                for m in providers.models_of(p["name"]):
                    data.append({"id": m["id"], "object": "model",
                                 "owned_by": "loom",
                                 "provider": p["name"],
                                 "state": st.get("state") or "unknown"})
            self._json(200, {"object": "list", "data": data})
        elif path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._err(404, f"no such route: {path}")

    def do_POST(self):
        if self._reject_unauthed():
            return
        path = self.path.split("?")[0]
        if path not in PROXY_PATHS:
            self._err(404, f"no such route: {path} - this API serves "
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
        name = str((payload or {}).get("model") or "")
        prov, mid = self._route(name)
        if prov is None:
            known = [f"{p['name']}/{m['id']}"
                     for p in self.server.loom_providers()
                     for m in providers.models_of(p["name"])]
            self._err(404, f"unknown model {name!r} - known: "
                      + (", ".join(known) or "(none probed yet - open "
                         "the Providers tab in Loom)"))
            return
        # the upstream wants ITS model id, not our provider/model spelling
        if mid != name:
            payload = dict(payload or {})
            payload["model"] = mid
            body = json.dumps(payload).encode("utf-8")
        try:
            resp = providers.request(
                prov, "POST", path, body,
                {"Content-Type": "application/json"}, timeout=GEN_TIMEOUT)
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
            self._err(502, f"cannot reach provider {prov['name']}: "
                      f"{getattr(e, 'reason', e)}")
            return
        # stream the answer through - SSE chunks land as they arrive
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
                pass   # the client hung up - llama-server sees the close


# ---------------------------------------------------------------------------
# loom.yaml `api:` section - written non-destructively, like the models
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
    # key - then give back any trailing blank/comment padding, so the
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
