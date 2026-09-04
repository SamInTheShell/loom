"""MCP (Model Context Protocol) servers - external tool providers for chats.

Servers are DEFINED in loom.yaml (`mcp-servers:` - name, command, env; the
MCP Servers tab's wizard writes entries non-destructively). Each enabled
server is a child process speaking MCP's stdio transport: newline-
delimited JSON-RPC 2.0 on stdin/stdout. This module is a deliberately
minimal client: initialize → notifications/initialized → tools/list, then
tools/call per invocation. Server-initiated requests are answered with
"method not found" (we offer no sampling/roots), notifications ignored.

Tool names surface to the model - and to loom.yaml's
permission-modes.<mode>.tools - as   mcp_<server>_<tool>   (the tab shows
these real function names so per-mode overrides are predictable). The
DEFAULT permission of each tool is chosen in the tab and stored per
library in state.json; an explicit per-mode entry in loom.yaml wins.

Enabled == running: toggling a server on/off in the tab starts/stops the
process AND records the set per library - servers running when the app
closed autostart when the library opens again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

from loom import store


class McpError(Exception):
    pass


INIT_TIMEOUT = 30
LIST_TIMEOUT = 30
CALL_TIMEOUT = 300
PROTOCOL_VERSION = "2025-03-26"

_lock = threading.Lock()
_clients: dict[str, "_Client"] = {}   # server name -> live client
_lib_root: str = ""                   # current library (store key)
_push = None                          # bus push, set by set_library


def full_tool_name(server: str, tool: str) -> str:
    return f"mcp_{server}_" + re.sub(r"[^\w-]", "_", str(tool))


class _Client:
    """One MCP server child process + its JSON-RPC plumbing."""

    def __init__(self, rec: dict):
        self.rec = dict(rec)
        self.name = rec["name"]
        self.tools: list[dict] = []       # raw MCP tool records
        self.dead = ""                    # non-empty = death reason
        self._id = 0
        self._pending: dict[int, list] = {}   # id -> [Event, message]
        self._wlock = threading.RLock()   # re-entrant: request() holds it
                                          # around the id bump AND the write
        env = dict(os.environ)
        env.update(rec.get("env") or {})
        try:
            self.proc = subprocess.Popen(
                ["bash", "-c", rec["command"]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, start_new_session=True)
        except OSError as e:
            raise McpError(f"cannot spawn {self.name!r}: {e}")
        self._stderr_tail: list[bytes] = []
        threading.Thread(target=self._read_stderr, daemon=True,
                         name=f"mcp-err-{self.name}").start()
        threading.Thread(target=self._reader, daemon=True,
                         name=f"mcp-{self.name}").start()

    # ---- transport ----
    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue   # startup banners etc. - not ours to parse
                try:
                    self._dispatch(msg)
                except Exception:
                    pass   # a reply we cannot send must not kill the reader
        except (OSError, ValueError):
            pass
        err = b"".join(self._stderr_tail).decode("utf-8", "replace").strip()
        self._die("the server process exited"
                  + (f" - {err[-400:]}" if err else ""))

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr_tail.append(line)
                del self._stderr_tail[:-30]
        except (OSError, ValueError):
            pass

    def _dispatch(self, msg: dict):
        mid = msg.get("id")
        if mid is not None and ("result" in msg or "error" in msg):
            slot = self._pending.get(mid)
            if slot:
                slot[1] = msg
                slot[0].set()
            return
        if mid is not None and msg.get("method"):
            # a server-initiated REQUEST (sampling, roots, elicitation…):
            # we offer none of it - answer politely so the server moves on
            if msg["method"] == "ping":
                self._write({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                self._write({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": "loom offers no client "
                    "capabilities"}})
        # notifications: ignored

    def _write(self, obj: dict):
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                raise McpError(f"{self.name}: {self.dead or 'server gone'}")

    def _die(self, why: str):
        self.dead = self.dead or why
        for slot in list(self._pending.values()):
            slot[1] = {"error": {"message": self.dead}}
            slot[0].set()

    # ---- rpc ----
    def request(self, method: str, params: dict, timeout: float) -> dict:
        if self.dead:
            raise McpError(f"{self.name}: {self.dead}")
        with self._wlock:
            self._id += 1
            rid = self._id
        ev = threading.Event()
        self._pending[rid] = [ev, None]
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                         "params": params})
            if not ev.wait(timeout):
                raise McpError(f"{self.name}: {method} timed out "
                               f"({int(timeout)}s)")
            msg = self._pending[rid][1] or {}
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            e = msg["error"] or {}
            raise McpError(f"{self.name}: "
                           + str(e.get("message") or e)[:500])
        return msg.get("result") if isinstance(msg.get("result"), dict) else {}

    def notify(self, method: str, params: dict | None = None):
        self._write({"jsonrpc": "2.0", "method": method,
                     "params": params or {}})

    # ---- lifecycle ----
    def initialize(self):
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "loom", "version": "1.0"},
        }, INIT_TIMEOUT)
        self.notify("notifications/initialized")
        self.refresh_tools()

    def refresh_tools(self):
        got = self.request("tools/list", {}, LIST_TIMEOUT)
        tools = got.get("tools")
        self.tools = [t for t in tools if isinstance(t, dict)
                      and t.get("name")] if isinstance(tools, list) else []

    def call(self, tool: str, args: dict) -> str:
        res = self.request("tools/call",
                           {"name": tool, "arguments": args or {}},
                           CALL_TIMEOUT)
        texts = []
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(str(c.get("text") or ""))
        out = "\n".join(t for t in texts if t)
        if not out and isinstance(res.get("structuredContent"), dict):
            out = json.dumps(res["structuredContent"], indent=2)
        if res.get("isError"):
            raise McpError(out or f"{self.name}.{tool} reported an error")
        return out or "(no content)"

    def stop(self):
        self._die("stopped")
        try:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# registry

def set_library(root: str, push=None) -> None:
    global _lib_root, _push
    _lib_root = str(root or "")
    if push is not None:
        _push = push


def _emit(**kw):
    p = _push
    if p is None:
        return
    try:
        p({"type": "mcp", **kw})
    except Exception:
        pass


def start_server(rec: dict) -> None:
    """Start one configured server, blocking through initialize +
    tools/list. Raises McpError with the stderr tail on failure."""
    name = rec["name"]
    with _lock:
        if name in _clients and not _clients[name].dead:
            return
        _clients.pop(name, None)
    client = _Client(rec)
    try:
        client.initialize()
    except McpError:
        client.stop()
        raise
    with _lock:
        _clients[name] = client
    _emit(kind="started", name=name)


def stop_server(name: str) -> None:
    with _lock:
        client = _clients.pop(str(name), None)
    if client is not None:
        client.stop()
    _emit(kind="stopped", name=str(name))


def shutdown() -> None:
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for c in clients:
        c.stop()


def running() -> dict[str, str]:
    """{name: "" | death reason} for every live-ish client."""
    with _lock:
        return {n: c.dead for n, c in _clients.items()}


def refresh_tools(name: str) -> int:
    with _lock:
        client = _clients.get(str(name))
    if client is None or client.dead:
        raise McpError(f"{name} is not running")
    client.refresh_tools()
    return len(client.tools)


def autostart(cfg_servers: list[dict]) -> None:
    """Start the servers recorded as running when the library last closed.
    Best effort - a failure surfaces as a bus event, not an exception."""
    want = set(store.mcp_running(_lib_root))
    for rec in cfg_servers or []:
        if rec["name"] not in want:
            continue
        try:
            start_server(rec)
        except McpError as e:
            _emit(kind="error", name=rec["name"], msg=str(e))


# ---------------------------------------------------------------------------
# permissions + the chat-facing tool surface

def tool_perm(full_name: str) -> str:
    """The tab-chosen DEFAULT level for one mcp_* tool (ask when unset).
    loom.yaml permission-modes overrides are applied by the caller."""
    level = store.mcp_tool_perms(_lib_root).get(str(full_name))
    return level if level in ("allow", "ask", "deny", "disabled") else "ask"


def set_tool_perm(full_name: str, level: str) -> None:
    store.set_mcp_tool_perm(_lib_root, str(full_name), str(level))


def live_tool_specs() -> list[dict]:
    """OpenAI-style function specs for every tool on every RUNNING server
    (permission filtering is the caller's job - it owns cfg + mode)."""
    specs = []
    with _lock:
        clients = [c for c in _clients.values() if not c.dead]
    for c in clients:
        for t in c.tools:
            schema = t.get("inputSchema")
            if not isinstance(schema, dict) or not schema:
                schema = {"type": "object", "properties": {}}
            specs.append({
                "type": "function",
                "function": {
                    "name": full_tool_name(c.name, t["name"]),
                    "description": (str(t.get("description") or "")[:800]
                                    or f"{t['name']} (MCP server "
                                       f"{c.name!r})"),
                    "parameters": schema,
                }})
    return specs


def call_full(full_name: str, args: dict) -> str:
    """Execute mcp_<server>_<tool> - resolves against live clients."""
    with _lock:
        clients = [c for c in _clients.values() if not c.dead]
    for c in clients:
        for t in c.tools:
            if full_tool_name(c.name, t["name"]) == full_name:
                return c.call(t["name"], args)
    raise McpError(f"no running MCP server offers {full_name} - check the "
                   "MCP Servers tab")


def status(cfg_servers: list[dict]) -> list[dict]:
    """Tab rows: config x live state, tools with their REAL function
    names and effective default permissions."""
    live = {}
    with _lock:
        live = dict(_clients)
    perms = store.mcp_tool_perms(_lib_root)
    out = []
    for rec in cfg_servers or []:
        c = live.get(rec["name"])
        tools = []
        if c is not None and not c.dead:
            for t in c.tools:
                full = full_tool_name(rec["name"], t["name"])
                lv = perms.get(full)
                tools.append({
                    "name": t["name"], "fullName": full,
                    "description": str(t.get("description") or "")[:300],
                    "perm": lv if lv in ("allow", "ask", "deny", "disabled")
                    else "ask"})
        out.append({"name": rec["name"], "command": rec["command"],
                    "env": rec.get("env") or {},
                    "running": c is not None and not c.dead,
                    "error": (c.dead if c is not None else ""),
                    "tools": tools})
    return out


# ---------------------------------------------------------------------------
# loom.yaml `mcp-servers:` injection (wizard) - same non-destructive rules
# as the models block: entries land at the end, everything else untouched

_MCP_KEY_RE = re.compile(r"^mcp-servers:\s*(\[\s*\])?\s*(#.*)?$")


def entry_lines(name: str, command: str, env: dict | None = None) -> list[str]:
    name = str(name or "").strip()
    if not re.match(r"^[\w-]+$", name):
        raise McpError("give the server a name - letters, digits, - and _ "
                       "only (it becomes part of tool function names)")
    command = str(command or "").strip()
    if not command:
        raise McpError("give the server a command line")
    q = lambda s: ('"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
                   if not re.match(r"^[\w~/. @:=-]+$", s) or s.strip() != s
                   else s)
    lines = [f"- name: {name}", f"  command: {q(command)}"]
    if env:
        lines.append("  env:")
        for k, v in env.items():
            lines.append(f"    {q(str(k))}: {q(str(v))}")
    return lines


def inject_server(text: str, name: str, command: str,
                  env: dict | None = None) -> str:
    new = entry_lines(name, command, env)
    src = text.splitlines()
    key_idx = next((i for i, ln in enumerate(src)
                    if _MCP_KEY_RE.match(ln)), None)
    if key_idx is None:
        out = src[:]
        if out and out[-1].strip():
            out.append("")
        return "\n".join(out + ["mcp-servers:"] + new) + "\n"
    m = _MCP_KEY_RE.match(src[key_idx])
    comment = (" " + m.group(2)) if m.group(2) else ""
    out = src[:key_idx] + [f"mcp-servers:{comment}"]
    rest = src[key_idx + 1:]
    if m.group(1):   # `mcp-servers: []`
        return "\n".join(out + new + rest) + "\n"
    j = 0
    while j < len(rest):
        ln = rest[j]
        if ln.strip() == "" or ln.lstrip().startswith("#") \
                or ln.startswith(" ") or ln.startswith("-"):
            j += 1
            continue
        break
    k = j
    while k > 0 and (rest[k - 1].strip() == ""
                     or rest[k - 1].lstrip().startswith("#")):
        k -= 1
    return "\n".join(out + rest[:k] + new + rest[k:]) + "\n"
