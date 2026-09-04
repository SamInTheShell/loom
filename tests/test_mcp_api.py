"""MCP servers + the OpenAI-compatible API server.

MCP: config parsing, yaml injection, a REAL stdio round-trip against a
stub MCP server, permission layering (tab default vs loom.yaml override).
API: config parsing/injection, routing (models list, unknown model,
stopped model), and a REAL proxy round-trip to a llama-server stand-in on
a unix socket - embeddings included.

Run: uv run python tests/test_mcp_api.py
"""

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
os.environ["HOME"] = tempfile.mkdtemp(prefix="loomtest-userhome-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from loom import apiserver, chat, libconfig, mcp, providers, store  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# =========================================================================
# libconfig: api + mcp-servers sections
cfg_api = libconfig._api(None)
check("api defaults", cfg_api == {"interface": "127.0.0.1", "port": 1234})
check("api parsed", libconfig._api({"interface": "0.0.0.0", "port": 9999})
      == {"interface": "0.0.0.0", "port": 9999})
for bad in ({"port": "x"}, {"port": 0}, {"port": 70000}):
    try:
        libconfig._api(bad)
        check(f"api rejects {bad}", False)
    except libconfig.ConfigError:
        check(f"api rejects {bad}", True)

got = libconfig._mcp_servers([{"name": "files", "command": "npx x",
                               "env": {"A": 1}}])
check("mcp-servers parsed",
      got == [{"name": "files", "command": "npx x", "env": {"A": "1"}}],
      str(got))
for bad in ([{"command": "x"}], [{"name": "a b", "command": "x"}],
            [{"name": "a"}], "nope",
            [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}]):
    try:
        libconfig._mcp_servers(bad)
        check(f"mcp rejects {str(bad)[:30]}", False)
    except libconfig.ConfigError:
        check(f"mcp rejects {str(bad)[:30]}", True)

# =========================================================================
# apiserver: loom.yaml injection
base = "providers: []\n# tail comment\nchat:\n  model: ''\n"
t1 = apiserver.inject_api_config(base, "127.0.0.1", 4321)
check("api inject appends a block",
      yaml.safe_load(t1)["api"] == {"interface": "127.0.0.1", "port": 4321}
      and "# tail comment" in t1, t1)
t2 = apiserver.inject_api_config(t1, "0.0.0.0", 5555)
check("api inject replaces in place",
      yaml.safe_load(t2)["api"] == {"interface": "0.0.0.0", "port": 5555}
      and t2.count("api:") == 1 and "# tail comment" in t2, t2)

# =========================================================================
# apiserver: live routing + proxy to a fake provider


class FakeLlama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # the API server re-probes stale providers on a routing miss -
        # answer like a real llama-server
        body = json.dumps({"object": "list", "data": [
            {"id": "test-model", "meta": {"n_ctx": 4096}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        out = json.dumps({"echo_path": self.path,
                          "echo": json.loads(body or b"{}")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


uhttpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama)
uhttpd.daemon_threads = True
threading.Thread(target=uhttpd.serve_forever, daemon=True).start()
_uport = uhttpd.server_address[1]

PROVIDERS = [{"name": "prov1", "type": "llama-cpp",
              "url": f"http://127.0.0.1:{_uport}", "ssh": ""}]
# the API routes by the CACHED model lists - seed the registry the way a
# probe would
with providers._lock:
    providers._providers["prov1"] = {
        "name": "prov1", "state": "ok", "detail": "",
        "models": [{"id": "test-model", "ctx": 4096}]}

check("api off at start", apiserver.status()["running"] is False)
got = apiserver.start("127.0.0.1", 0, lambda: PROVIDERS)
check("api starts", got["running"] and got["port"] > 0, str(got))
base_url = f"http://127.0.0.1:{got['port']}"


def get(path):
    with urllib.request.urlopen(base_url + path, timeout=10) as r:
        return r.status, json.loads(r.read())


def post(path, obj):
    req = urllib.request.Request(base_url + path,
                                 data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


code, body = get("/v1/models")
check("GET /v1/models lists every provider's models",
      code == 200 and [m["id"] for m in body["data"]] == ["test-model"]
      and body["data"][0]["provider"] == "prov1", str(body))

code, body = post("/v1/chat/completions",
                  {"model": "test-model", "messages": []})
check("chat completions proxied to the provider",
      code == 200 and body["echo_path"] == "/v1/chat/completions"
      and body["echo"]["model"] == "test-model", str(body))

code, body = post("/v1/chat/completions",
                  {"model": "prov1/test-model", "messages": []})
check("provider/model spelling routes and rewrites the id",
      code == 200 and body["echo"]["model"] == "test-model", str(body))

code, body = post("/v1/embeddings", {"model": "test-model", "input": "hi"})
check("embeddings proxied",
      code == 200 and body["echo_path"] == "/v1/embeddings"
      and body["echo"]["input"] == "hi", str(body))

code, body = post("/v1/chat/completions", {"model": "gpt-4o", "messages": []})
check("unknown model with ONE known model routes to it",
      code == 200 and body["echo"]["model"] == "test-model", str(body))

code, body = post("/v1/nope", {})
check("unknown route → 404", code == 404)

apiserver.stop()
check("api stops", apiserver.status()["running"] is False)

# ---- API key: everything except /health requires it ----
got = apiserver.start("127.0.0.1", 0, lambda: PROVIDERS, api_key="sekret")
check("keyed start reports it", got["running"] and got["keyed"] is True,
      str(got))
base_url = f"http://127.0.0.1:{got['port']}"


def get_h(path, headers=None):
    req = urllib.request.Request(base_url + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


code, body = get_h("/v1/models")
check("no key → 401 with a hint",
      code == 401 and "API key" in body["error"]["message"], str(body))
code, body = get_h("/v1/models", {"Authorization": "Bearer sekret"})
check("Bearer key admits", code == 200, str(body))
code, body = get_h("/v1/models", {"x-api-key": "sekret"})
check("x-api-key admits", code == 200)
code, body = get_h("/v1/models", {"Authorization": "Bearer wrong"})
check("wrong key → 401", code == 401)
code, body = get_h("/health")
check("/health stays public (reachability probes need no secret)",
      code == 200 and body["status"] == "ok", str(body))
req = urllib.request.Request(
    base_url + "/v1/chat/completions",
    data=json.dumps({"model": "test-model", "messages": []}).encode(),
    headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
check("POST without key → 401", code == 401)

apiserver.stop()
check("stop clears the key", apiserver.status()["keyed"] is False)
uhttpd.shutdown()
with providers._lock:
    providers._providers.clear()

# =========================================================================
# provider-side API keys ride every provider request as Bearer


class EchoAuth(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps(
            {"auth": self.headers.get("Authorization") or ""}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


eauth = ThreadingHTTPServer(("127.0.0.1", 0), EchoAuth)
eauth.daemon_threads = True
threading.Thread(target=eauth.serve_forever, daemon=True).start()
_eport = eauth.server_address[1]
_eprov = {"name": "kp", "type": "llama-cpp",
          "url": f"http://127.0.0.1:{_eport}", "ssh": ""}


def _echo_auth(prov):
    with providers.request(prov, "GET", "/x") as r:
        return json.loads(r.read())["auth"]


check("no key: no auth header", _echo_auth(_eprov) == "")
check("inline record key rides as Bearer",
      _echo_auth({**_eprov, "key": "abc"}) == "Bearer abc")
providers.set_key_resolver(lambda n: "resolved" if n == "kp" else "")
check("resolver key rides as Bearer",
      _echo_auth(_eprov) == "Bearer resolved")
check("inline key beats the resolver",
      _echo_auth({**_eprov, "key": "abc"}) == "Bearer abc")
providers.set_key_resolver(None)
check("cleared resolver: open again", _echo_auth(_eprov) == "")
eauth.shutdown()

# =========================================================================
# mcp: yaml injection
t3 = mcp.inject_server(base, "files", "npx -y server-fs /tmp", {"A": "b c"})
parsed = libconfig._mcp_servers(yaml.safe_load(t3).get("mcp-servers"))
check("mcp inject creates the block",
      parsed == [{"name": "files", "command": "npx -y server-fs /tmp",
                  "env": {"A": "b c"}}] and "# tail comment" in t3, t3)
t4 = mcp.inject_server(t3, "web", "uvx mcp-server-fetch", None)
parsed = libconfig._mcp_servers(yaml.safe_load(t4).get("mcp-servers"))
check("mcp inject appends to the block",
      [s["name"] for s in parsed] == ["files", "web"], t4)
for name, cmd in (("bad name", "x"), ("ok", "")):
    try:
        mcp.inject_server(base, name, cmd)
        check(f"mcp inject rejects {name!r}/{cmd!r}", False)
    except mcp.McpError:
        check(f"mcp inject rejects {name!r}/{cmd!r}", True)

# =========================================================================
# mcp: a real stdio round-trip against a stub server
STUB = r'''
import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    meth, mid = m.get("method"), m.get("id")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": m["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "1"}}})
    elif meth == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "echoes back",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}}]}})
    elif meth == "tools/call":
        p = m["params"]
        if p["name"] == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text",
                 "text": "echo: " + p["arguments"]["text"]}]}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32602, "message": "no such tool"}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
'''
stub_path = Path(tempfile.mkdtemp(prefix="loomtest-mcp-")) / "stub.py"
stub_path.write_text(STUB)

LIB = str(Path(os.environ["HOME"]) / "lib")
mcp.set_library(LIB)
rec = {"name": "stub", "command": f"{sys.executable} {stub_path}", "env": {}}
mcp.start_server(rec)
check("mcp server starts and lists tools",
      mcp.running() == {"stub": ""}, str(mcp.running()))
specs = mcp.live_tool_specs()
check("tool spec surfaces with the full function name",
      len(specs) == 1 and specs[0]["function"]["name"] == "mcp_stub_echo"
      and specs[0]["function"]["parameters"]["required"] == ["text"],
      str(specs))
out = mcp.call_full("mcp_stub_echo", {"text": "hi"})
check("tools/call round-trips", out == "echo: hi", out)
check("refresh re-queries", mcp.refresh_tools("stub") == 1)
try:
    mcp.call_full("mcp_stub_missing", {})
    check("unknown mcp tool raises", False)
except mcp.McpError:
    check("unknown mcp tool raises", True)

# permission layering: tab default vs loom.yaml per-mode override
CFG = {"permissionModes": {"always-ask": {"mcp_stub_echo": "deny"},
                           "custom": {}},
       "chat": {"permission_mode": "always-ask"}}
check("tab default is ask", chat.perm_for(CFG, "mcp_stub_echo", "custom") == "ask")
mcp.set_tool_perm("mcp_stub_echo", "allow")
check("tab default applies where yaml is silent",
      chat.perm_for(CFG, "mcp_stub_echo", "custom") == "allow")
check("loom.yaml per-mode override wins",
      chat.perm_for(CFG, "mcp_stub_echo", "always-ask") == "deny")
check("built-in tools resolve through libconfig",
      chat.perm_for({"permissionModes": libconfig.BUILTIN_MODES,
                     "chat": {"permission_mode": "always-ask"}},
                    "read_file") == "allow")
mcp.set_tool_perm("mcp_stub_echo", "disabled")
specs = chat.tool_specs(CFG, "custom")
check("disabled mcp tool is not offered",
      all(s["function"]["name"] != "mcp_stub_echo" for s in specs))
mcp.set_tool_perm("mcp_stub_echo", "")
check("clearing restores ask", mcp.tool_perm("mcp_stub_echo") == "ask")

# enabled == running persistence (the autostart set)
store.set_mcp_running(LIB, "stub", True)
check("running set persists", store.mcp_running(LIB) == ["stub"])
store.set_mcp_running(LIB, "stub", False)
check("disable clears it", store.mcp_running(LIB) == [])

mcp.stop_server("stub")
check("mcp server stops", mcp.running() == {}, str(mcp.running()))

# =========================================================================
from loom import chats  # noqa: E402
import threading as _t  # noqa: E402

chat_root = Path(tempfile.mkdtemp(prefix="loomtest-chatroot-"))

# a cancelled worker still blocked waiting for headers (stop() raced the
# abort registration) is freed when wait_if_cancelling re-fires the close
freed = _t.Event()


class FakeStream:
    def close(self):
        freed.set()          # ...which unblocks the "worker" below


th = _t.Thread(target=freed.wait, args=(10,), daemon=True)
th.start()
cancel_ev = _t.Event()
cancel_ev.set()
with chat._lock:
    chat._running["stuck"] = {"thread": th, "cancel": cancel_ev}
    chat._streams["stuck"] = FakeStream()
check("wait_if_cancelling re-fires the abort and frees the worker",
      chat.wait_if_cancelling("stuck", timeout=5.0) and freed.is_set())
live_ev = _t.Event()
live_th = _t.Thread(target=live_ev.wait, args=(10,), daemon=True)
live_th.start()
with chat._lock:
    chat._running["live"] = {"thread": live_th, "cancel": _t.Event()}
check("an uncancelled live stream still bounces immediately",
      chat.wait_if_cancelling("live") is False)
live_ev.set()
with chat._lock:
    chat._running.clear()
    chat._streams.clear()

# a mid-stream model switch on disk reaches the in-memory doc (and is
# therefore never clobbered by the worker's next save)
doc_m = chats.new_chat(chat_root, model="Old Model")
mem = chats.load_chat(chat_root, doc_m["id"])
doc_m["model"] = "New Model"
chats.save_chat(chat_root, doc_m)
chat._refresh_user_fields(chat_root, mem)
check("mid-stream model switch survives the worker's save cycle",
      mem["model"] == "New Model", str(mem.get("model")))

# join_worker: gone workers return True fast; live ones wait
check("join_worker with no worker", chat.join_worker("nope") is True)
jev = _t.Event()
jth = _t.Thread(target=jev.wait, args=(10,), daemon=True)
jth.start()
with chat._lock:
    chat._running["j"] = {"thread": jth, "cancel": _t.Event()}
check("join_worker times out on a live worker",
      chat.join_worker("j", timeout=0.2) is False)
jev.set()
check("join_worker returns once the worker dies",
      chat.join_worker("j", timeout=5.0) is True)
with chat._lock:
    chat._running.clear()

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
