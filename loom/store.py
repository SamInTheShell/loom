"""App-data layout and state persistence under ~/.loom.

Loom's own data is small — the library concept means almost everything the
user cares about (prompts, knowledge, config, chats) lives INSIDE the
selected library folder. What remains here is app-level:

    ~/.loom/
      state.json           theme + the recent-libraries list
      run/                 unix sockets, transient state (askpass, ssh mux)
      bin/                 the askpass helper executable
      loom.log             stdout/stderr when detached (not used yet)

Server STATE (pid, socket, log) lives on the host that runs the server,
under ~/.loom/servers/<id>/ THERE — see srv.py.

The recents list drives the library picker:
    recents: [{path, lastOpened, omitted}]
`omitted` folders stay in the file (so the omission is durable) but are
never shown — re-opening one requires a manual Select, which un-omits it
only if the user asks.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_ENV_HOME = "LOOM_HOME"  # tests point this at a temp dir


def home() -> Path:
    return Path(os.environ.get(_ENV_HOME, "~/.loom")).expanduser()


def run_dir() -> Path:
    return home() / "run"


def bin_dir() -> Path:
    return home() / "bin"


def state_path() -> Path:
    return home() / "state.json"


def ensure_dirs() -> None:
    for d in (run_dir(), bin_dir()):
        d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(run_dir(), 0o700)
    except OSError:
        pass


DEFAULT_STATE = {
    "theme": "dark",
    "recents": [],   # [{path, lastOpened, omitted}]
    # per-library UI sessions, keyed by resolved library path:
    #   {tabs: [{type, chatId}], activeTab, lib: {open, expanded, leftWidth}}
    # — "where I left off" when a library is reopened
    "sessions": {},
    # per-library alert history, keyed by resolved library path:
    #   [{ts, level, msg}] — survives app restarts until the user clears it
    "alerts": {},
    # per-library reasoning preferences, keyed by resolved library path:
    #   {model id: {method, level}} — absent means "send nothing" (the
    #   server's own default), which is also what clearing restores
    "reasoning": {},
    # per-library pinned model ids (shown first in pickers), keyed by
    # resolved library path: [model id, ...] in pin order
    "modelPins": {},
    # per-ssh-host llama-server concurrency: {host: n} — how many servers
    # may run at once on that host ("" = this machine). Default 1 (model
    # RAM use is unmeasured — one at a time is the only safe default);
    # -1 = no limit. Machine state, not library config: the limit models
    # the host's RAM, which travels with the machine, not the library.
    "serverLimits": {},
    # per-library MCP state, keyed by resolved library path:
    #   mcpRunning:   [server names] — running at last close → autostart
    #   mcpToolPerms: {mcp_<server>_<tool>: level} — the tab-chosen
    #                 DEFAULT permission (loom.yaml per-mode entries win)
    "mcpRunning": {},
    "mcpToolPerms": {},
}


def load_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    merged = dict(DEFAULT_STATE)
    merged.update(st if isinstance(st, dict) else {})
    if not isinstance(merged.get("recents"), list):
        merged["recents"] = []
    merged["recents"] = [r for r in merged["recents"]
                         if isinstance(r, dict) and r.get("path")]
    if not isinstance(merged.get("sessions"), dict):
        merged["sessions"] = {}
    if not isinstance(merged.get("alerts"), dict):
        merged["alerts"] = {}
    if not isinstance(merged.get("reasoning"), dict):
        merged["reasoning"] = {}
    if not isinstance(merged.get("modelPins"), dict):
        merged["modelPins"] = {}
    if not isinstance(merged.get("serverLimits"), dict):
        merged["serverLimits"] = {}
    if not isinstance(merged.get("mcpRunning"), dict):
        merged["mcpRunning"] = {}
    if not isinstance(merged.get("mcpToolPerms"), dict):
        merged["mcpToolPerms"] = {}
    return merged


# state is written from several threads (JsApi bridge calls). One lock
# serializes whole read-modify-write cycles; the unique tmp name keeps even
# another process's writer from racing our rename.
_mutate_lock = threading.RLock()


def save_state(st: dict) -> None:
    ensure_dirs()
    tmp = state_path().with_suffix(
        f".json.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(state_path())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mutate_state(fn):
    """Atomic read-modify-write of state.json — fn(state) mutates in place
    (or returns a replacement). Returns the state as saved."""
    with _mutate_lock:
        st = load_state()
        out = fn(st)
        if isinstance(out, dict):
            st = out
        save_state(st)
        return st


# ---------------------------------------------------------------------------
# the recent-libraries list

def display_path(path: str) -> str:
    """A path for the eye: $HOME collapsed to ~."""
    home_dir = str(Path("~").expanduser())
    p = str(path)
    if p == home_dir:
        return "~"
    if p.startswith(home_dir + os.sep):
        return "~" + p[len(home_dir):]
    return p


def visible_recents() -> list[dict]:
    """Most-recent first, omitted entries excluded. Each entry carries a
    `display` path (~ for the home directory) for the picker."""
    rec = [dict(r, display=display_path(r.get("path") or ""))
           for r in load_state()["recents"] if not r.get("omitted")]
    rec.sort(key=lambda r: r.get("lastOpened") or 0, reverse=True)
    return rec


def touch_recent(path: str) -> None:
    """Record that `path` was opened. An omitted entry stays omitted — a
    manual Select opens it without resurrecting it in the list."""
    p = str(Path(path).expanduser().resolve())

    def fn(st):
        # monotonic: two opens in the same millisecond must still order
        ts = max(int(time.time() * 1000),
                 max((r.get("lastOpened") or 0 for r in st["recents"]),
                     default=0) + 1)
        for r in st["recents"]:
            if r.get("path") == p:
                r["lastOpened"] = ts
                return
        st["recents"].append({"path": p, "lastOpened": ts, "omitted": False})
    mutate_state(fn)


def remove_recent(path: str, omit: bool = False) -> None:
    """Right-click → Clear removes the entry; → Omit keeps it, flagged, so
    the folder never reappears in the list on future opens."""
    p = str(path)

    def fn(st):
        if omit:
            for r in st["recents"]:
                if r.get("path") == p:
                    r["omitted"] = True
                    return
            st["recents"].append({"path": p, "lastOpened": 0, "omitted": True})
        else:
            st["recents"] = [r for r in st["recents"] if r.get("path") != p]
    mutate_state(fn)


def session_get(path: str) -> dict | None:
    """The saved UI session for a library, or None."""
    s = load_state()["sessions"].get(str(path))
    return s if isinstance(s, dict) else None


def session_set(path: str, session: dict) -> None:
    """Persist a library's UI session (tabs, active tab, open file, ...)."""
    def fn(st):
        st.setdefault("sessions", {})[str(path)] = \
            session if isinstance(session, dict) else {}
    mutate_state(fn)


ALERTS_MAX = 500


def alerts_get(path: str) -> list[dict]:
    """The persisted alert log for a library, oldest first."""
    items = load_state()["alerts"].get(str(path))
    if not isinstance(items, list):
        return []
    return [a for a in items if isinstance(a, dict)]


def alerts_add(path: str, item: dict) -> None:
    """Append one alert to a library's log (capped at ALERTS_MAX)."""
    if not isinstance(item, dict):
        return
    entry = {"ts": item.get("ts") or int(time.time() * 1000),
             "level": str(item.get("level") or "info"),
             "msg": str(item.get("msg") or "")}
    if not entry["msg"]:
        return

    def fn(st):
        log = st.setdefault("alerts", {}).setdefault(str(path), [])
        if not isinstance(log, list):
            log = st["alerts"][str(path)] = []
        log.append(entry)
        del log[:-ALERTS_MAX]
    mutate_state(fn)


def alerts_clear(path: str) -> None:
    """Drop every kept alert for a library."""
    def fn(st):
        st.setdefault("alerts", {}).pop(str(path), None)
    mutate_state(fn)


# ---------------------------------------------------------------------------
# pinned models (per library) — pickers list these first

def pins_all(path: str) -> list[str]:
    p = load_state()["modelPins"].get(str(path))
    return [str(x) for x in p] if isinstance(p, list) else []


def pin_set(path: str, model_id: str, pinned: bool) -> list[str]:
    def fn(st):
        lib = st.setdefault("modelPins", {}).setdefault(str(path), [])
        if not isinstance(lib, list):
            lib = st["modelPins"][str(path)] = []
        mid = str(model_id)
        if pinned and mid not in lib:
            lib.append(mid)
        elif not pinned and mid in lib:
            lib.remove(mid)
    out = mutate_state(fn)
    return [str(x) for x in out["modelPins"].get(str(path), [])]


# ---------------------------------------------------------------------------
# per-model reasoning preferences (per library)

REASONING_METHODS = ("effort", "template", "prompt")
REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max",
                    "on", "off")


def reasoning_all(path: str) -> dict:
    """{model id: {method, level}} for a library. Absent = default."""
    m = load_state()["reasoning"].get(str(path))
    if not isinstance(m, dict):
        return {}
    return {k: v for k, v in m.items()
            if isinstance(v, dict) and v.get("method") in REASONING_METHODS
            and v.get("level") in REASONING_LEVELS}


def reasoning_get(path: str, model_id: str) -> dict | None:
    return reasoning_all(path).get(str(model_id))


def reasoning_set(path: str, model_id: str, pref: dict | None) -> None:
    """Set (or, with None, clear back to default) one model's preference."""
    def fn(st):
        lib = st.setdefault("reasoning", {}).setdefault(str(path), {})
        if not isinstance(lib, dict):
            lib = st["reasoning"][str(path)] = {}
        if not pref:
            lib.pop(str(model_id), None)
            return
        method = str(pref.get("method") or "")
        level = str(pref.get("level") or "")
        if method not in REASONING_METHODS or level not in REASONING_LEVELS:
            raise ValueError(f"bad reasoning pref: {pref!r}")
        lib[str(model_id)] = {"method": method, "level": level}
    mutate_state(fn)


def clear_recents() -> None:
    """Clear the visible list. Omitted entries stay — that flag is the
    user's durable 'never show this' decision, not a cache."""
    def fn(st):
        st["recents"] = [r for r in st["recents"] if r.get("omitted")]
    mutate_state(fn)


# ---------------------------------------------------------------------------
# per-host llama-server concurrency limits

SERVER_LIMIT_MAX = 16


def server_limits() -> dict:
    got = load_state().get("serverLimits")
    return {str(k): v for k, v in got.items()} if isinstance(got, dict) else {}


def server_limit(host: str) -> int:
    """How many llama-servers may run at once on `host` ('' = local).
    Default 1; -1 means no limit."""
    try:
        n = int(server_limits().get(str(host or ""), 1))
    except (TypeError, ValueError):
        return 1
    return n if n == -1 or 1 <= n <= SERVER_LIMIT_MAX else 1


def set_server_limit(host: str, n: int) -> dict:
    """Persist a host's limit. Returns the full limits map."""
    n = int(n)
    if n != -1 and not (1 <= n <= SERVER_LIMIT_MAX):
        raise ValueError(
            f"the limit must be 1-{SERVER_LIMIT_MAX}, or -1 for no limit")

    def fn(st):
        cur = st.get("serverLimits")
        cur = cur if isinstance(cur, dict) else {}
        cur[str(host or "")] = n
        st["serverLimits"] = cur
    return mutate_state(fn).get("serverLimits", {})


# ---------------------------------------------------------------------------
# MCP: which servers autostart, and the tab-chosen default tool permissions

def mcp_running(path: str) -> list[str]:
    got = load_state()["mcpRunning"].get(str(path))
    return [str(n) for n in got] if isinstance(got, list) else []


def set_mcp_running(path: str, name: str, on: bool) -> list[str]:
    def fn(st):
        lib = st.setdefault("mcpRunning", {})
        cur = lib.get(str(path))
        cur = [str(n) for n in cur] if isinstance(cur, list) else []
        if on and str(name) not in cur:
            cur.append(str(name))
        if not on:
            cur = [n for n in cur if n != str(name)]
        lib[str(path)] = cur
    return mutate_state(fn)["mcpRunning"].get(str(path), [])


def mcp_tool_perms(path: str) -> dict:
    got = load_state()["mcpToolPerms"].get(str(path))
    return {str(k): str(v) for k, v in got.items()} \
        if isinstance(got, dict) else {}


def set_mcp_tool_perm(path: str, tool: str, level: str) -> None:
    """Set one tool's default level; '' clears back to ask."""
    if level not in ("allow", "ask", "deny", "disabled", ""):
        raise ValueError(f"not a permission level: {level!r}")

    def fn(st):
        lib = st.setdefault("mcpToolPerms", {}).setdefault(str(path), {})
        if not isinstance(lib, dict):
            lib = st["mcpToolPerms"][str(path)] = {}
        if level:
            lib[str(tool)] = level
        else:
            lib.pop(str(tool), None)
    mutate_state(fn)
