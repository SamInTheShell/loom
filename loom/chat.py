"""The chat loop - streaming inference against a configured provider
(llama.cpp's llama-server or ninfer, over HTTP, optionally through an ssh
tunnel), with tools, loom.yaml-driven permissions, and container-backed
shell.

Message flow mirrors llama.cpp's web chat: one streaming POST to
/v1/chat/completions per turn (SSE), deltas pushed to the page as they
arrive. `timings_per_token` + `stream_options.include_usage` +
`return_progress` are injected so token stats, LIVE tok/s, and prompt-
processing progress are always present (both llama-server and ninfer
speak these llama.cpp-style extensions; servers that don't simply omit
the fields).

Agent loop: stream a turn → if the model called tools, resolve each
against loom.yaml permissions (allow / ask / deny), run the allowed ones,
append the results, stream again - until a turn ends with no tool calls.

THREADING: send() runs on a daemon worker (never a pywebview bridge
thread). stop() sets the chat's cancel event AND closes the live SSE
response so a blocked socket read unblocks immediately.

Paths the model sees are the CONTAINER's view: attached folders are
/mnt/<name> (read-only for view mode, read-write for write mode), the
library knowledge base is /knowledge (read-only; `knowledge/...` also
accepted in file tools), and the chat's ARTIFACT folder is /artifacts
(read-write): anything the model leaves there is delivered back to the
user as a chat attachment (folders as zip downloads). The same layout
holds in shell commands, so the shell doubles as read-only tooling over
view-mode data.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import threading
import time
import traceback
import urllib.error
from pathlib import Path

from loom import (chats, containers, envs, libconfig, library, mcp,
                  providers, search, store)

# seconds without a chunk = stall. Generous: a CPU prefill over a huge
# context can legitimately emit nothing for minutes (though servers that
# speak return_progress keep the progress chunks flowing meanwhile).
STREAM_IDLE_TIMEOUT = 600
MAX_TOOL_RESULT = 60_000


# --------------------------------------------------------------------------
# runtime registries

_lock = threading.Lock()
_running: dict[str, dict] = {}    # chat_id -> {cancel, thread}
_streams: dict[str, object] = {}  # chat_id -> live HTTPResponse | _PreStream


class _PreStream:
    """Registered in _streams for the window BETWEEN sending a request
    and receiving response headers (a busy server can sit there for
    minutes). stop() calls .close() on whatever is registered - this
    shim aborts the underlying connection so that wait ends NOW."""

    def __init__(self, box: dict):
        self._box = box

    def close(self):
        fn = self._box.get("abort")
        if fn:
            try:
                fn()
            except Exception:
                pass


# everything a dying/aborted HTTP stream can throw mid-iteration -
# including AttributeError: closing a response another thread is reading
# leaves http.client poking a None fp ("'NoneType' ... 'peek'")
STREAM_ERRORS = (OSError, urllib.error.URLError, http.client.HTTPException,
                 AttributeError, ValueError)


def is_running(chat_id: str) -> bool:
    with _lock:
        rec = _running.get(chat_id)
        return bool(rec and rec["thread"].is_alive())


def running_chats() -> list[str]:
    with _lock:
        return [cid for cid, r in _running.items() if r["thread"].is_alive()]


def wait_if_cancelling(chat_id: str, timeout: float = 4.0) -> bool:
    """True once the chat's worker is gone. A stream the user already
    CANCELLED is merely unwinding - briefly wait it out so a send racing
    the cancel succeeds instead of bouncing with 'already streaming'. A
    live, uncancelled stream returns False immediately.

    The abort is RE-FIRED here: stop() may have raced the request setup
    (its close hit the _PreStream before the abort callable existed),
    leaving a cancelled worker blocked waiting for first-token headers.
    Closing whatever is registered NOW is idempotent and frees it."""
    with _lock:
        rec = _running.get(chat_id)
    if rec is None or not rec["thread"].is_alive():
        return True
    if not rec["cancel"].is_set():
        return False
    with _lock:
        resp = _streams.get(chat_id)
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
    rec["thread"].join(timeout)
    return not rec["thread"].is_alive()


def join_worker(chat_id: str, timeout: float = 6.0) -> bool:
    """Wait for the chat's worker thread to end (True when gone). A
    delete must not race the dying worker's FINAL save - that would
    resurrect the file the delete just removed."""
    with _lock:
        rec = _running.get(chat_id)
    if rec is None:
        return True
    rec["thread"].join(timeout)
    return not rec["thread"].is_alive()


def stop(chat_id: str) -> bool:
    with _lock:
        rec = _running.get(chat_id)
        resp = _streams.get(chat_id)
    if rec:
        rec["cancel"].set()
    GATE.cancel_chat(chat_id)
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
    return bool(rec)


class _Gate:
    """Blocks a tool between the permission question and the user's answer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._waiting: dict[str, dict] = {}  # call_id -> {ev, decision, chat, tool}

    def prepare(self, chat_id: str, call_id: str, tool: str = ""):
        with self._lock:
            self._waiting[call_id] = {"ev": threading.Event(),
                                      "decision": None, "chat": chat_id,
                                      "tool": tool}

    def pending_for(self, chat_id: str) -> list[tuple[str, str]]:
        """(call_id, tool name) for every call still waiting in a chat -
        so a permission-mode switch can re-decide them."""
        with self._lock:
            return [(cid, r.get("tool") or "")
                    for cid, r in self._waiting.items()
                    if r["chat"] == chat_id and not r["ev"].is_set()]

    def wait(self, call_id: str, cancel: threading.Event) -> str:
        with self._lock:
            rec = self._waiting.get(call_id)
        if rec is None:
            return "deny"
        while not rec["ev"].wait(0.2):
            if cancel.is_set():
                break
        with self._lock:
            self._waiting.pop(call_id, None)
        return rec["decision"] or "deny"

    def answer(self, call_id: str, decision: str) -> bool:
        with self._lock:
            rec = self._waiting.get(call_id)
        if rec is None:
            return False
        rec["decision"] = "allow" if decision == "allow" else "deny"
        rec["ev"].set()
        return True

    def cancel_chat(self, chat_id: str) -> None:
        with self._lock:
            recs = [r for r in self._waiting.values() if r["chat"] == chat_id]
        for r in recs:
            r["decision"] = "deny"
            r["ev"].set()


GATE = _Gate()


# --------------------------------------------------------------------------
# tools

# read_file refuses whole files past ~25k tokens (~4 chars/token) - the
# model must read big files in offset/limit slices instead
READ_GATE_CHARS = 100_000
READ_SLICE_MAX_LINES = 2000


def perm_for(cfg: dict | None, name: str, mode: str = "",
             chat_perms: dict | None = None) -> str:
    """The effective level for one tool. mcp_* tools resolve in three
    layers: the CHAT's own per-tool override (the tools bar's MCP panel)
    wins; then an explicit entry in the active permission mode
    (loom.yaml); then the default chosen in the MCP Servers tab
    (libconfig's blanket unknown-tool 'ask' never sees mcp_* names)."""
    if name.startswith("mcp_"):
        got = (chat_perms or {}).get(name)
        if got in libconfig.PERM_LEVELS:
            return got
        modes = (cfg or {}).get("permissionModes") or {}
        m = mode or ((cfg or {}).get("chat") or {}).get("permission_mode") \
            or libconfig.DEFAULT_MODE
        tools = modes.get(m)   # an empty mode is still THAT mode
        if not isinstance(tools, dict):
            tools = modes.get(libconfig.DEFAULT_MODE) or {}
        if name in tools:
            return tools[name]
        return mcp.tool_perm(name)
    return libconfig.permission_for(cfg, name, mode) if cfg else "allow"


def tool_specs(cfg: dict | None = None, mode: str = "",
               network=None, artifacts: bool = True,
               knowledge: bool = True,
               mcp_perms: dict | None = None) -> list[dict]:
    """The tools offered this turn - built-ins plus every tool of every
    RUNNING MCP server. A tool at level `disabled` in the active
    permission mode is not offered at all. Every chat has the
    write tools and shell: /artifacts is writable (unless the chat's
    artifacts chip disabled it - `artifacts` False drops it from every
    description), and view-mode mounts are enforced read-only by the
    container itself.

    `network` (when known: none / loopback / on, legacy bools accepted)
    is spelled out in the shell tool's own description - the model reads
    tool specs far more reliably than a system-prompt aside, and
    troubleshooting phantom connectivity is exactly the time waste that
    line prevents."""
    def level(name):
        return perm_for(cfg, name, mode, mcp_perms)

    specs: list[dict] = []

    def add(name, desc, params, required):
        if level(name) == "disabled":
            return
        specs.append({"type": "function",
                      "function": {"name": name, "description": desc,
                                   "parameters": {"type": "object",
                                                  "properties": params,
                                                  "required": required}}})

    if knowledge:
        add("knowledge_search",
            "Search the library's markdown knowledge base. Returns "
            "matching files and lines with paths like /knowledge/foo.md.",
            {"query": {"type": "string"}}, ["query"])
    kb_and = " and the knowledge base" if knowledge else ""
    kb_scope = "'/knowledge', or " if knowledge else ""
    add("grep",
        "Search file CONTENTS (case-insensitive substring, ranked) across "
        "the attached folders" + kb_and + ". Returns "
        "path:line: text hits. Scope with `path`: '/' (everything), "
        + kb_scope + "'/mnt/<folder>[/sub]'.",
        {"query": {"type": "string"},
         "path": {"type": "string", "description": "scope, default '/'"}},
        ["query"])
    add("find_files",
        "Fuzzy-find FILES by name/path (subsequence match, like Ctrl+P) "
        "across the attached folders" + kb_and + ". Scope with "
        "`path` like grep.",
        {"query": {"type": "string"},
         "path": {"type": "string", "description": "scope, default '/'"}},
        ["query"])
    add("read_file",
        "Read a text file. Paths: "
        + ("/knowledge/<...> for the knowledge base, " if knowledge else "")
        + "/mnt/<folder>/<...> for attached folders"
        + (", /artifacts/<...> for this chat's artifact folder"
           if artifacts else "")
        + ". Files over ~25k tokens "
        "refuse a whole-file read - pass offset (1-based line) and limit "
        "(line count) to read a slice; grep first to find the right spot.",
        {"path": {"type": "string"},
         "offset": {"type": "integer", "description": "1-based start line"},
         "limit": {"type": "integer", "description": "max lines to return"}},
        ["path"])
    add("list_dir",
        "List a directory. Use '/' to see what is attached.",
        {"path": {"type": "string"}}, ["path"])
    add("edit_file",
        "Edit a text file by exact string replacement: old_string must "
        "match the file contents exactly and be UNIQUE (or set "
        "replace_all true). Works inside write-mode attached folders"
        + (" and /artifacts" if artifacts else "")
        + ". Prefer this over write_file for existing files.",
        {"path": {"type": "string"},
         "old_string": {"type": "string"},
         "new_string": {"type": "string"},
         "replace_all": {"type": "boolean"}},
        ["path", "old_string", "new_string"])
    add("write_file",
        "Create or overwrite a text file inside a write-mode attached "
        "folder (/mnt/<folder>/<...>)"
        + (" or /artifacts. Files and folders you place in /artifacts "
           "are delivered to the user as chat attachments." if artifacts
           else "."),
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"])
    # nmode, NOT mode - `mode` is the permission mode the level() closure
    # reads; shadowing it silently re-enabled disabled tools once.
    # SIGNAL ONLY WHEN RESTRICTED: "online" is what every model assumes,
    # so network-on says nothing - the signal exists to correct the
    # assumption, not to confirm it.
    nmode = containers.net_mode(network) if network is not None else None
    if nmode == "on":
        net_line = ""
    elif nmode == "loopback":
        net_line = ("This chat's container has LOOPBACK-ONLY networking: "
                    "the HOST machine's 127.0.0.1 services are reachable "
                    "at 10.0.2.2 (use that address, not localhost, for "
                    "host services), and servers you start inside the "
                    "container work on its own localhost. The wider "
                    "internet is NOT reachable - downloads and package "
                    "installs WILL fail by configuration, not by bug. ")
    elif nmode == "none":
        net_line = ("This chat's container has NO NETWORK ACCESS "
                    "(off by default): DNS, curl/wget, pip/npm/apt "
                    "installs and git fetches WILL fail with connection "
                    "errors. That is configuration, not a bug - do not "
                    "troubleshoot connectivity; ask the user to enable "
                    "the network chip if a command needs it. Servers you "
                    "start inside the container ARE reachable from the "
                    "container's own localhost. ")
    else:
        net_line = "No network unless the user enabled it for this chat. "
    add("shell",
        "Run a bash command inside the sandbox container (unprivileged "
        "user). " + net_line
        + "Attached folders are under /mnt (view mode mounts read-only)"
        + (", the knowledge base is at /knowledge (read-only)"
           if knowledge else "")
        + (", and /artifacts is read-write - anything left there is "
           "delivered to the user. " if artifacts else
           ". This chat's /artifacts delivery folder is DISABLED - it is "
           "not mounted; do not write there. ")
        + "ALWAYS set `timeout` to fit the command - a hung command runs "
        "until the timeout kills it.",
        {"command": {"type": "string"},
         "timeout": {"type": "integer",
                     "description": "REQUIRED - seconds before the "
                     "command is killed. Size it to the job: ~10-30 for "
                     "quick commands, more only for builds/tests that "
                     "genuinely need it (max 3600)."}},
        ["command", "timeout"])
    for spec in mcp.live_tool_specs():
        if level(spec["function"]["name"]) != "disabled":
            specs.append(spec)
    return specs


# the classic spellings of "the network isn't there" across curl, pip,
# npm, git, apt, and raw getaddrinfo/connect failures
_NET_ERR_RE = None


def _looks_network_error(output: str) -> bool:
    import re
    global _NET_ERR_RE
    if _NET_ERR_RE is None:
        _NET_ERR_RE = re.compile(
            r"could ?n.t resolve|name or service not known"
            r"|temporary failure in name resolution|network is unreachable"
            r"|connection (refused|timed out|reset)|no route to host"
            r"|failed to (fetch|connect)|getaddrinfo|EAI_AGAIN"
            r"|ENOTFOUND|ETIMEDOUT|ECONNREFUSED|ENETUNREACH",
            re.IGNORECASE)
    return bool(_NET_ERR_RE.search(str(output or "")))


def _mount_map(chat: dict) -> dict[str, Path]:
    """container mount name -> host folder path (mirrors containers.mounts_for)."""
    out: dict[str, Path] = {}
    for f in chat.get("folders") or []:
        p = Path(str(f.get("path") or "")).expanduser()
        if not p.is_dir():
            continue
        name = containers._mount_name(str(p))
        n, i = name, 2
        while n in out:
            n, i = f"{name}-{i}", i + 1
        out[n] = p.resolve()
    return out


def _folder_mode(chat: dict, host: Path) -> str:
    for f in chat.get("folders") or []:
        try:
            if Path(str(f.get("path"))).expanduser().resolve() == host:
                return str(f.get("mode") or "view")
        except OSError:
            continue
    return "view"


def _resolve_path(root: Path, chat: dict, path: str) -> tuple[str, Path, str]:
    """('knowledge'|'mount'|'artifact', host path, mode). Refuses escapes."""
    p = str(path or "").strip()
    if p in ("", "/"):
        return ("root", root, "view")
    if p.startswith("/knowledge"):        # the container spelling
        p = p.lstrip("/") or "knowledge"
    if p.startswith("knowledge/") or p == "knowledge":
        if chat.get("knowledgeOff"):
            raise chats.ChatError(
                "the knowledge base is disabled for this chat - ask the "
                "user to flip the knowledge chip if it is needed")
        host = library.safe_join(root, p)
        return ("knowledge", host, "view")
    if p == "/artifacts" or p.startswith("/artifacts/"):
        if chat.get("artifactsOff"):
            raise chats.ChatError(
                "artifacts are disabled for this chat - ask the user to "
                "flip the artifacts chip if a file should be delivered")
        base = chats.artifacts_dir(root, str(chat["id"]), create=True)
        rel = p[len("/artifacts/"):] if len(p) > len("/artifacts") else ""
        host = (base / rel).resolve() if rel else base
        if host != base and base not in host.parents:
            raise chats.ChatError(f"path escapes /artifacts: {path}")
        return ("artifact", host, "write")
    if p.startswith("/mnt/"):
        parts = p[5:].split("/", 1)
        mounts = _mount_map(chat)
        base = mounts.get(parts[0])
        if base is None:
            raise chats.ChatError(f"no attached folder named {parts[0]!r}")
        host = (base / parts[1]).resolve() if len(parts) > 1 else base
        if host != base and base not in host.parents:
            raise chats.ChatError(f"path escapes the attached folder: {path}")
        return ("mount", host, _folder_mode(chat, base))
    raise chats.ChatError(
        f"unknown path {path!r} - use /knowledge/<...>, /mnt/<folder>/<...>, "
        "or /artifacts/<...>")


def _search_scopes(root: Path, chat: dict, path: str) -> list[tuple[str, Path]]:
    """(display prefix, host dir) pairs for a container-view scope path:
    '/' = knowledge + every mount, else one resolved directory."""
    p = str(path or "/").strip() or "/"
    if p == "/":
        scopes = []
        if not chat.get("knowledgeOff"):
            scopes.append(("/knowledge/", root / "knowledge"))
        if not chat.get("artifactsOff"):
            scopes.append(("/artifacts/",
                           chats.artifacts_dir(root, str(chat["id"]))))
        for n, hp in _mount_map(chat).items():
            scopes.append((f"/mnt/{n}/", hp))
        return [(d, h) for d, h in scopes if h.is_dir()]
    _kind, host, _mode = _resolve_path(root, chat, p)
    if not host.is_dir():
        raise chats.ChatError(f"not a directory: {path}")
    return [(p.rstrip("/") + "/", host)]


def _exec_tool(root: Path, cfg: dict, chat: dict, name: str, args: dict,
               cancel: threading.Event, on_output=None) -> str:
    if name.startswith("mcp_"):
        try:
            return mcp.call_full(name, args)
        except mcp.McpError as e:
            raise chats.ChatError(str(e))

    if name in ("grep", "find_files"):
        query = str(args.get("query") or "").strip()
        if not query:
            raise chats.ChatError("give grep/find_files a query")
        want = "line" if name == "grep" else "file"
        hits = []
        for prefix, base in _search_scopes(root, chat, str(args.get("path") or "/")):
            for h in search.search(base, query, limit=120):
                if h["kind"] != want:
                    continue
                if want == "line":
                    hits.append((h["score"], f"{prefix}{h['rel']}:{h['line']}: {h['text']}"))
                else:
                    hits.append((h["score"], f"{prefix}{h['rel']}"))
        hits.sort(key=lambda x: -x[0])
        if not hits:
            return "no matches"
        return "\n".join(t for _s, t in hits[:60])

    if name == "edit_file":
        kind, host, mode = _resolve_path(root, chat, str(args.get("path") or ""))
        if kind not in ("mount", "artifact") or mode != "write":
            raise chats.ChatError(
                "edit_file only works inside write-mode attached folders "
                "or /artifacts")
        if not host.is_file():
            raise chats.ChatError(f"no such file: {args.get('path')}")
        data = host.read_bytes()
        if library.looks_binary(data):
            raise chats.ChatError("that file is binary")
        text = data.decode("utf-8", "replace")
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if not old:
            raise chats.ChatError("old_string is empty")
        n = text.count(old)
        if n == 0:
            raise chats.ChatError("old_string not found in the file - it "
                                  "must match the current contents exactly")
        if n > 1 and not args.get("replace_all"):
            raise chats.ChatError(
                f"old_string matches {n} places - extend it until it is "
                "unique, or set replace_all: true")
        out = text.replace(old, new) if args.get("replace_all") \
            else text.replace(old, new, 1)
        host.write_text(out, encoding="utf-8")
        return f"replaced {n if args.get('replace_all') else 1} occurrence(s)"

    if name == "knowledge_search":
        if chat.get("knowledgeOff"):
            raise chats.ChatError(
                "the knowledge base is disabled for this chat")
        hits = search.search(root, str(args.get("query") or ""),
                             subdir="knowledge", limit=30)
        if not hits:
            return "no matches"
        lines = []
        # container-view paths, consistent with every other tool: the
        # knowledge base is /knowledge, artifacts are /artifacts, mounts
        # are /mnt/<name>
        for h in hits:
            if h["kind"] == "file":
                lines.append(f"file: /{h['rel']}")
            else:
                lines.append(f"/{h['rel']}:{h['line']}: {h['text']}")
        return "\n".join(lines)

    if name == "list_dir":
        kind, host, _mode = _resolve_path(root, chat, str(args.get("path") or "/"))
        if kind == "root":
            mounts = _mount_map(chat)
            out = []
            if not chat.get("knowledgeOff"):
                out.append("/knowledge/  (library knowledge base, "
                           "read-only)")
            if not chat.get("artifactsOff"):
                out.append("/artifacts/  (read-write - files here are "
                           "delivered to the user)")
            for n, p in mounts.items():
                mode = _folder_mode(chat, p)
                out.append(f"/mnt/{n}/  ({'read-write' if mode == 'write' else 'read-only'})")
            return "\n".join(out)
        if not host.is_dir():
            raise chats.ChatError(f"not a directory: {args.get('path')}")
        rows = []
        for p in sorted(host.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            rows.append(p.name + ("/" if p.is_dir() else ""))
        return "\n".join(rows) or "(empty)"

    if name == "read_file":
        _kind, host, _mode = _resolve_path(root, chat, str(args.get("path") or ""))
        if not host.is_file():
            raise chats.ChatError(f"no such file: {args.get('path')}")
        if host.stat().st_size > 20_000_000:
            raise chats.ChatError("file is over 20 MB")
        data = host.read_bytes()
        if library.looks_binary(data):
            raise chats.ChatError("that file is binary")
        text = data.decode("utf-8", "replace")
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is None and limit is None:
            if len(text) > READ_GATE_CHARS:
                lines_total = text.count("\n") + 1
                raise chats.ChatError(
                    f"the file is ~{len(text) // 4} tokens ({lines_total} "
                    "lines) - too large for a whole-file read. Pass offset "
                    "(1-based line) and limit (line count) to read a "
                    "slice, and grep to find the right region first")
            return text
        lines = text.split("\n")
        try:
            start = max(1, int(offset or 1))
            count = int(limit or READ_SLICE_MAX_LINES)
        except (TypeError, ValueError):
            raise chats.ChatError("offset/limit must be integers")
        count = max(1, min(count, READ_SLICE_MAX_LINES))
        sel = lines[start - 1:start - 1 + count]
        body = "\n".join(sel)
        if len(body) > READ_GATE_CHARS:
            body = body[:READ_GATE_CHARS] + "\n[slice truncated - use a smaller limit]"
        return (f"[lines {start}-{start + len(sel) - 1} of {len(lines)}]\n"
                + body)

    if name == "write_file":
        kind, host, mode = _resolve_path(root, chat, str(args.get("path") or ""))
        if kind not in ("mount", "artifact") or mode != "write":
            raise chats.ChatError(
                "write_file only works inside write-mode attached folders "
                "or /artifacts")
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_text(str(args.get("content") or ""), encoding="utf-8")
        return f"wrote {len(str(args.get('content') or ''))} bytes"

    if name == "shell":
        engine, image = containers.ensure_image_named(
            root, cfg, str(chat.get("container") or ""), notice=on_output)
        extra_env = None
        if chat.get("env"):
            try:
                extra_env, _missing = envs.resolve(root, str(chat["env"]))
                extra_env = extra_env or None
            except envs.EnvError as e:
                raise chats.ChatError(str(e))
        try:
            timeout_s = int(args.get("timeout")
                            or containers.EXEC_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            timeout_s = containers.EXEC_TIMEOUT_DEFAULT
        timeout_s = max(1, min(timeout_s, containers.EXEC_TIMEOUT_MAX))
        net = containers.net_mode(chat.get("network"))
        res = containers.run_shell(
            engine, image, str(chat["id"]), str(args.get("command") or ""),
            folders=chat.get("folders") or [],
            timeout=timeout_s,
            cancel=cancel, on_output=on_output,
            network=net,
            knowledge=None if chat.get("knowledgeOff")
            else root / "knowledge",
            artifacts=None if chat.get("artifactsOff")
            else chats.artifacts_dir(root, str(chat["id"]), create=True),
            extra_env=extra_env)
        # the shell has THREE honest outcomes, and the card must show the
        # right one: ok (exit 0) / failed (nonzero exit, timeout) /
        # cancelled (the user stopped it). Returning only text used to
        # collapse all three into "ok".
        tail = ""
        status = "ok" if res.rc == 0 else "failed"
        if res.timed_out:
            # naming the budget lets the model pick a better one next try
            tail = f"\n[timed out after {timeout_s}s - pass a larger " \
                   "`timeout` if the command genuinely needs longer]"
            status = "failed"
        elif res.cancelled:
            tail = "\n[cancelled]"
            status = "cancelled"
        out = f"exit {res.rc}{tail}\n{res.output}"
        # a failure that LOOKS like a connectivity problem in a
        # restricted container gets named for what it is, right in the
        # result - the moment a model would otherwise start a long,
        # doomed connectivity hunt
        if status == "failed" and net != "on" \
                and _looks_network_error(res.output):
            out += ("\n[note: this chat's container has LOOPBACK-ONLY "
                    "networking - host 127.0.0.1 services are at 10.0.2.2; "
                    "the wider internet is unreachable by configuration, "
                    "not by bug. Ask the user to set the network chip to "
                    "full access if this command needs the internet.]"
                    if net == "loopback" else
                    "\n[note: this chat's container has NO NETWORK ACCESS "
                    "(the network chip is off) - network operations fail "
                    "by configuration, not by bug. Ask the user to change "
                    "the network chip if this command needs it.]")
        return out, status

    raise chats.ChatError(f"unknown tool: {name}")


# --------------------------------------------------------------------------
# artifacts: the model's delivery folder

ARTIFACT_SCAN_MAX = 2000   # files walked per artifact folder for size/mtime

def _artifact_records(root: Path, chat_id: str) -> list[dict]:
    """Top-level entries of the chat's artifact folder (the user-facing
    attachments). `uploads/` - the user's own attachments staged for the
    container - is not echoed back."""
    base = chats.artifacts_dir(root, chat_id)
    out: list[dict] = []
    if not base.is_dir():
        return out
    for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith(".") or p.name == "uploads":
            continue
        try:
            if p.is_dir():
                size, walked = 0, 0
                mtime = p.stat().st_mtime
                for f in p.rglob("*"):
                    walked += 1
                    if walked > ARTIFACT_SCAN_MAX:
                        break
                    try:
                        fst = f.stat()
                    except OSError:
                        continue
                    if f.is_file():
                        size += fst.st_size
                    mtime = max(mtime, fst.st_mtime)
                out.append({"name": p.name, "dir": True, "bytes": size,
                            "mtime": int(mtime * 1000), "path": str(p)})
            else:
                fst = p.stat()
                out.append({"name": p.name, "dir": False,
                            "bytes": fst.st_size,
                            "mtime": int(fst.st_mtime * 1000),
                            "path": str(p)})
        except OSError:
            continue
    return out


def _sync_artifacts(root: Path, chat: dict, ev=None) -> list[str]:
    """Refresh chat['artifacts'] from disk; returns the names that are new
    or changed since the last sync (and announces them via ev).

    Fresh artifacts also land as an 'artifact' MESSAGE in the chat - the
    timeline shows when each delivery happened, and the message keeps its
    own open/save affordances even after the pill is dismissed. A fresh
    delivery un-dismisses its pill (a regenerated file is news again)."""
    if chat.get("artifactsOff"):
        return []   # the chip disabled deliveries for this chat
    recs = _artifact_records(root, str(chat["id"]))
    old = {a.get("name"): a for a in chat.get("artifacts") or []}
    fresh = [r["name"] for r in recs
             if r["name"] not in old
             or old[r["name"]].get("mtime") != r["mtime"]
             or old[r["name"]].get("bytes") != r["bytes"]]
    chat["artifacts"] = recs
    if fresh:
        dismissed = [n for n in chat.get("artifactsDismissed") or []
                     if n not in fresh]
        if dismissed:
            chat["artifactsDismissed"] = dismissed
        else:
            chat.pop("artifactsDismissed", None)
        by_name = {r["name"]: r for r in recs}
        chat["messages"].append({
            "role": "artifact",
            "items": [{"name": n, "dir": bool(by_name[n].get("dir")),
                       "bytes": int(by_name[n].get("bytes") or 0)}
                      for n in fresh if n in by_name],
            "ts": int(time.time() * 1000)})
    if fresh and ev:
        ev("artifacts", items=recs, fresh=fresh)
    return fresh


# --------------------------------------------------------------------------
# wire

def _read_prompt(root: Path, rel: str, fallback: str) -> str:
    try:
        return library.safe_join(root, str(rel)).read_text(encoding="utf-8")
    except (OSError, library.LibraryError):
        return fallback


def _active_slice(chat: dict) -> tuple[dict | None, list[dict]]:
    """(latest compaction marker | None, messages after it). Everything
    before the marker stays in the FILE (history view) but leaves the
    wire - the summary stands in for it."""
    msgs = chat.get("messages") or []
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "compact":
            return msgs[i], msgs[i + 1:]
    return None, msgs


def _truncate_thoughts(cfg: dict, chat: dict) -> bool:
    """Per-chat thought truncation; loom.yaml's chat.thought_truncation
    is only the default stamped onto NEW chats (and the fallback for
    chats from before the per-chat setting existed)."""
    v = chat.get("thoughtTruncation")
    if v is None:
        v = (cfg.get("chat") or {}).get("thought_truncation", True)
    return bool(v)


_ECHOED_STAMP_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\]\s*")


def _strip_echoed_stamp(text: str) -> str:
    """A model that sees its own history stamped `[.. UTC]` imitates the
    prefix in fresh replies. The signal belongs to Loom, not the visible
    message - drop the echo; the wire re-adds the authoritative stamp."""
    return _ECHOED_STAMP_RE.sub("", text, count=1)


def _utc_stamp(ms=None) -> str:
    import datetime
    dt = datetime.datetime.now(datetime.timezone.utc) if ms is None \
        else datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _knowledge_map(root: Path) -> list[str]:
    """A two-level map of knowledge/ so the model KNOWS what the base
    covers instead of having to guess that it exists."""
    base = root / "knowledge"
    out = []
    try:
        for d in sorted(base.iterdir()):
            if d.name.startswith(".") or not d.is_dir():
                continue
            subs = sorted(s.name for s in d.iterdir()
                          if s.is_dir() and not s.name.startswith("."))
            files = sum(1 for f in d.rglob("*.md"))
            detail = (", ".join(subs[:8])) if subs else f"{files} docs"
            out.append(f"- /knowledge/{d.name}/ - {detail}")
        loose = sorted(f.name for f in base.glob("*.md"))
        if loose and not out:
            out.append("- " + ", ".join("/knowledge/" + n for n in loose[:10]))
    except OSError:
        pass
    return out


def _wire_messages(root: Path, cfg: dict, chat: dict) -> list[dict]:
    sysp = _read_prompt(root,
                        (cfg.get("chat") or {}).get("system_prompt")
                        or "prompts/system.md",
                        "You are a helpful assistant.")
    mounts = _mount_map(chat)
    # PREFIX STABILITY: llama-server's prompt cache reuses KV only up to
    # the first changed token - a live clock here forced a FULL reprocess
    # of the whole context every turn. The session stamp never changes;
    # "now" comes from the newest user message's bracket stamp instead.
    # the time-travel panel's master switch: signals OFF means the model
    # receives NO datetime signal anywhere - no session stamp, no
    # message brackets on either side
    sig_off = bool(chat.get("timeSignalsOff"))
    asst_sig = not sig_off and bool(
        (cfg.get("chat") or {}).get("assistant_signals", True))
    ctx = ["", "## Environment"]
    if not sig_off:
        ctx.append(
            f"Session started {_utc_stamp(chat.get('createdTs'))}. All "
            "timestamps are UTC; "
            + ("every message begins with its UTC time in [brackets] - a "
               "user message's send time, or the time your own reply was "
               "generated" if asst_sig else
               "every user message begins with its send time in [brackets]")
            + " - the newest one is the current time.")
    # the knowledge base signal is AUTO-DETAILED: what it is, how access
    # works, and a live map of its contents - unless the chat's
    # knowledge chip cut the whole thing off
    kb_off = bool(chat.get("knowledgeOff"))
    if not kb_off:
        kmap = _knowledge_map(root)
        ctx.append("")
        ctx.append("## Knowledge base")
        ctx.append("This library ships a curated knowledge base: practices "
                   "and reference material meant to be consulted, not "
                   "guessed at. Before answering anything it may cover, "
                   "call knowledge_search with a few keywords; hits come "
                   "back as /knowledge/<path>[:line] - read the matching "
                   "file with read_file. Folder READMEs map their contents; "
                   "read them first when they match.")
        if kmap:
            ctx.append("It currently contains:")
            ctx += kmap
        else:
            ctx.append("It is currently empty - say so rather than "
                       "citing it.")
    ctx.append("")
    if mounts:
        ctx.append("Attached folders (also visible in shell commands):")
        for n, p in mounts.items():
            mode = _folder_mode(chat, p)
            ctx.append(f"- /mnt/{n} ({'read-write' if mode == 'write' else 'read-only'})")
    cont = str(chat.get("container") or "") \
        or str((cfg.get("containers") or {}).get("default") or "sandbox")
    net_mode = containers.net_mode(chat.get("network"))
    if net_mode == "on":
        # no network signal at all: online is the assumed default -
        # only a RESTRICTED network is worth words
        net_text = (f"Shell commands run in the '{cont}' container as an "
                    "unprivileged user.")
    elif net_mode == "loopback":
        net_text = (f"Shell commands run in the '{cont}' container as an "
                    "unprivileged user with LOOPBACK-ONLY networking: the "
                    "HOST machine's 127.0.0.1 services are reachable at "
                    "10.0.2.2 (use that address for host services, not "
                    "localhost), servers started inside the container work "
                    "on the container's own localhost, and the wider "
                    "internet is NOT reachable - downloads and package "
                    "installs WILL fail by configuration, not by bug. If "
                    "a command genuinely needs the internet, say so and "
                    "ask the user to set the network chip to full access.")
    else:
        # spelled out hard: a model that doesn't KNOW the network is off
        # burns whole turns troubleshooting phantom connectivity
        net_text = (f"Shell commands run in the '{cont}' container as an "
                    "unprivileged user, with NO NETWORK ACCESS (the chat's "
                    "network chip, off by default). DNS lookups, "
                    "curl/wget, package installs (pip/npm/apt) and git "
                    "fetches WILL fail with connection errors - that is "
                    "configuration, not a bug. Never troubleshoot "
                    "connectivity; if a command genuinely needs the "
                    "network, say so and ask the user to change the "
                    "network chip (loopback-only reaches host services; "
                    "full access reaches the internet). Servers started "
                    "inside the container ARE reachable from the "
                    "container's own localhost.")
    ctx.append(net_text
               + ("" if kb_off else " The knowledge base is mounted "
                  "read-only at /knowledge;")
               + " view-mode folders are mounted read-only - the "
               "shell is safe for traversing and inspecting them.")
    if chat.get("env"):
        try:
            resolved, missing = envs.resolve(root, str(chat["env"]))
        except envs.EnvError:
            resolved, missing = {}, []
        # per-variable SIGNAL hiding (the tools bar's env panel): hidden
        # variables still load into containers - the model just isn't
        # told their names
        hidden = set(chat.get("envHidden") or [])
        shown = sorted(n for n in resolved if n not in hidden)
        miss_shown = [n for n in missing if n not in hidden]
        if shown or miss_shown:
            line = (f"The environment '{chat['env']}' is loaded into shell "
                    "containers.")
            if shown:
                line += (" Set (values hidden here): "
                         + ", ".join(shown) + ".")
            if miss_shown:
                line += (" MISSING on this machine - secret stubs with no "
                         "local value, so these are UNSET: "
                         + ", ".join(miss_shown) + ".")
            ctx.append(line)
    if not chat.get("artifactsOff"):
        ctx.append("")
        ctx.append("## Artifacts")
        ctx.append("/artifacts is this chat's read-write delivery folder "
                   "(shell and file tools). Anything you place there is "
                   "handed to the user as a chat attachment - folders "
                   "become zip downloads. The user's uploaded files for "
                   "this chat are under /artifacts/uploads/.")
    out = [{"role": "system", "content": sysp + "\n".join(ctx)}]
    compact, live = _active_slice(chat)
    if compact is not None:
        out.append({"role": "user", "content":
                    "[Summary of the earlier conversation - the context was "
                    "compacted]\n" + str(compact.get("content") or "")})
    # thought truncation (default on): only the LAST assistant turn carries
    # its thinking back into the context; older thoughts are dropped
    truncate = _truncate_thoughts(cfg, chat)
    last_asst = max((i for i, m in enumerate(live)
                     if m.get("role") == "assistant"), default=-1)
    for i, m in enumerate(live):
        role = m.get("role")
        if role == "user":
            content = _user_content(m)
            # timestamp signal: every user message carries its UTC send
            # time - or its time-travel SIGNAL time (signalTs), stamped at
            # send when the chat's time-travel offset was armed
            sig = m.get("signalTs") or m.get("ts")
            if sig and not sig_off:
                stamp = f"[{_utc_stamp(sig)}] "
                if isinstance(content, str):
                    content = stamp + content
                elif isinstance(content, list):
                    txt = next((p for p in content
                                if p.get("type") == "text"), None)
                    if txt is not None:
                        txt["text"] = stamp + (txt.get("text") or "")
                    else:
                        content.insert(0, {"type": "text",
                                           "text": stamp.strip()})
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            content = m.get("content") or ""
            # datetime signal (chat.assistant_signals): the reply carries
            # the time it was generated - the SIGNAL time when time
            # travel was armed. Prefixes the visible text; the think
            # block stays first, the way models produce it.
            sig = m.get("signalTs") or m.get("ts")
            if asst_sig and sig:
                content = f"[{_utc_stamp(sig)}] " + content
            think = m.get("thinking")
            if think and (not truncate or i == last_asst):
                content = f"<think>\n{think}\n</think>\n{content}"
            w = {"role": "assistant", "content": content}
            if m.get("tool_calls"):
                w["tool_calls"] = m["tool_calls"]
            out.append(w)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                        "content": m.get("content") or ""})
    return out


_IMG_MAGIC = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg"}


def _to_png(data: bytes) -> bytes | None:
    """Transcode webp/gif/bmp to PNG via Qt - llama-server's image decoder
    (stb) takes png/jpeg reliably; other formats 400 or fail to decode."""
    try:
        from qtpy.QtCore import QBuffer, QByteArray
        from qtpy.QtGui import QImage
        img = QImage.fromData(data)
        if img.isNull():
            return None
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        out = bytes(ba)
        return out or None
    except Exception:
        return None


def _image_data_uri(path: str) -> str | None:
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError:
        return None
    for magic, mime in _IMG_MAGIC.items():
        if data.startswith(magic):
            return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    # webp / gif / bmp: accepted, transcoded to PNG for the server
    if (data[:4] == b"RIFF" and data[8:12] == b"WEBP") \
            or data[:4] in (b"GIF8",) or data[:2] == b"BM":
        png = _to_png(data)
        if png:
            return f"data:image/png;base64,{base64.b64encode(png).decode()}"
    return None


def _user_content(m: dict):
    text = m.get("content") or ""
    images = m.get("images") or []
    if not images:
        return text
    parts = [{"type": "text", "text": text}] if text else []
    for img in images:
        uri = _image_data_uri(str(img.get("path") or ""))
        if uri:
            parts.append({"type": "image_url", "image_url": {"url": uri}})
    return parts or text


def _sse(resp):
    """Yield parsed JSON chunks from an SSE stream until [DONE]/EOF."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def resolve_endpoint(cfg: dict, chat: dict, probe: bool = False) -> dict:
    """The provider record + model id this chat talks to:
    {provider: <config record>, model: <model id>, name: <display>}.

    The chat's own choice wins; the config's chat.provider/chat.model is
    the default; the first configured provider (and its first cached
    model) is the last resort.

    probe=True (the SEND path only) may hit the network to fill an empty
    model-list cache. Read paths (context chips, diagnostics) must never
    block on a dead host - they resolve from the cache or raise."""
    provs = cfg.get("providers") or []
    if not provs:
        raise chats.ChatError(
            "loom.yaml defines no providers yet - add a `providers:` "
            "entry pointing at a llama-server or ninfer API")
    pname = str(chat.get("provider") or "") \
        or str((cfg.get("chat") or {}).get("provider") or "")
    prov = libconfig.provider_by_name(cfg, pname)
    if prov is None:
        raise chats.ChatError(f"no provider named {pname!r} in loom.yaml")
    cached = providers.models_of(prov["name"])
    model = str(chat.get("model") or "")
    if not model and not chat.get("provider"):
        # only inherit the config default model when the provider is the
        # default too - a hand-picked provider gets ITS first model
        model = str((cfg.get("chat") or {}).get("model") or "")
    if not model:
        if not cached and probe:
            cached = providers.probe(prov).get("models") or []
        if not cached:
            raise chats.ChatError(
                f"{prov['name']} lists no models - is the server up? "
                f"({providers.status_of(prov['name']).get('detail') or 'no probe yet'})")
        model = str(cached[0]["id"])
    elif cached and model not in [m["id"] for m in cached]:
        # a KNOWN model list without this id: a stale/renamed model. A
        # single-model llama-server would silently generate with whatever
        # it has loaded - and worse, auto-compaction would silently
        # disarm (no context size for an unknown id). Say so instead.
        raise chats.ChatError(
            f"{prov['name']} does not list a model named {model!r} "
            f"(it serves: {', '.join(m['id'] for m in cached)}) - "
            "pick a model in the composer (Ctrl+.)")
    return {"provider": prov, "model": model,
            "name": f"{prov['name']} · {model}"}


# --------------------------------------------------------------------------
# context accounting + compaction

def _est(text) -> int:
    """chars/4 - the classic rough token estimate."""
    return (len(str(text or "")) + 3) // 4


def _nctx_for(cfg: dict, chat: dict) -> int:
    """The context window of the chat's model, from the provider's own
    metadata (cached by the last probe). 0 = unknown."""
    try:
        ep = resolve_endpoint(cfg, chat)
    except chats.ChatError:
        return 0
    return providers.model_ctx(ep["provider"]["name"], ep["model"])


def _last_used_tokens(chat: dict) -> int:
    """The real context size after the last completed turn: that turn's
    prompt_tokens + completion_tokens, from llama-server's usage object.
    Only the LIVE slice counts - usage from before a compaction marker
    describes a context that no longer exists, and letting it linger kept
    usedTokens pinned at the pre-compaction value (which made the chip lie
    and auto-compaction re-fire for nothing)."""
    _compact, live = _active_slice(chat)
    for m in reversed(live):
        u = m.get("usage")
        if m.get("role") == "assistant" and isinstance(u, dict):
            return int(u.get("prompt_tokens") or 0) \
                + int(u.get("completion_tokens") or 0)
    return 0


# how many recent turn-growth samples feed the adaptive headroom
TURN_WINDOW = 8


def _turn_growth(chat: dict) -> dict:
    """Recent per-turn context growth, from llama-server's REAL usage
    objects: {avg, max, n} tokens added per model turn over the last
    TURN_WINDOW turns. Deltas that span a compaction (totals shrink) are
    skipped. This is what lets auto-compaction anticipate a spiky turn
    instead of being shocked into a full window."""
    totals: list[int | None] = []
    for m in chat.get("messages") or []:
        if m.get("role") == "compact":
            totals.append(None)   # boundary - don't diff across it
            continue
        u = m.get("usage")
        if m.get("role") == "assistant" and isinstance(u, dict):
            totals.append(int(u.get("prompt_tokens") or 0)
                          + int(u.get("completion_tokens") or 0))
    deltas = [b - a for a, b in zip(totals, totals[1:])
              if a is not None and b is not None and b > a]
    recent = deltas[-TURN_WINDOW:]
    if not recent:
        return {"avg": 0, "max": 0, "n": 0}
    return {"avg": sum(recent) // len(recent), "max": max(recent),
            "n": len(recent)}


def _headroom(chat: dict, nctx: int) -> int:
    """Tokens the NEXT turn should be assumed to need: the recent worst
    case, padded average, or a 5% floor - whichever is largest."""
    g = _turn_growth(chat)
    return max(nctx // 20, g["max"], (g["avg"] * 3) // 2)


def context_breakdown(root: Path, cfg: dict, chat: dict) -> dict:
    """Estimated context composition (chars/4) for the hover breakdown,
    plus the last turn's REAL token count and the compaction settings."""
    ch = cfg.get("chat") or {}
    compact, live = _active_slice(chat)
    truncate = _truncate_thoughts(cfg, chat)
    sys_text = _read_prompt(root, ch.get("system_prompt") or "prompts/system.md",
                            "You are a helpful assistant.")
    parts = {"system": _est(sys_text) + 60,
             "compacted": _est(compact.get("content")) if compact else 0,
             "user": 0, "assistant": 0, "thoughts": 0, "toolResults": 0,
             "toolSpecs": 0}
    images = 0
    last_asst = max((i for i, m in enumerate(live)
                     if m.get("role") == "assistant"), default=-1)
    for i, m in enumerate(live):
        role = m.get("role")
        if role == "user":
            parts["user"] += _est(m.get("content")) + 4
            images += len(m.get("images") or [])
        elif role == "assistant":
            parts["assistant"] += _est(m.get("content")) + 4
            if m.get("thinking") and (not truncate or i == last_asst):
                parts["thoughts"] += _est(m.get("thinking"))
        elif role == "tool":
            parts["toolResults"] += _est(m.get("content")) + 4
    parts["toolSpecs"] = _est(json.dumps(
        tool_specs(cfg, str(chat.get("permMode") or ""),
                   network=containers.net_mode(chat.get("network")),
                   artifacts=not chat.get("artifactsOff"),
                   knowledge=not chat.get("knowledgeOff"),
                   mcp_perms=chat.get("mcpPerms"))))
    # hard guarantee for the UI: every part is an int, never null
    parts = {k: int(v or 0) for k, v in parts.items()}
    est_total = sum(parts.values())
    last_used = _last_used_tokens(chat)
    nctx = _nctx_for(cfg, chat)
    used = max(est_total, last_used)
    comp = ch.get("compaction") or {"auto": True, "threshold": 0.8}
    growth = _turn_growth(chat)
    speeds = _measured_speeds(chat)
    return {"parts": parts, "images": images, "estTokens": est_total,
            "lastUsedTokens": last_used, "usedTokens": used, "nCtx": nctx,
            "pct": round(100 * used / nctx, 1) if nctx else None,
            "auto": bool(comp.get("auto", True)),
            "threshold": float(comp.get("threshold", 0.8)),
            "turnAvg": growth["avg"], "turnMax": growth["max"],
            "turnSamples": growth["n"],
            "headroom": _headroom(chat, nctx) if nctx else 0,
            # measured on THIS chat's server: what the UI needs to say
            # "your next prompt is ~N tok and will take ~T s to process"
            "promptSpeed": speeds["prompt"], "genSpeed": speeds["gen"],
            "lastCachedTokens": speeds["cached"],
            "liveMessages": len(live), "compacted": compact is not None}


def _measured_speeds(chat: dict) -> dict:
    """{prompt, gen, cached} from the newest real timings in the chat:
    prompt tok/s (prefill), generation tok/s, and how many prompt tokens
    the cache absorbed last turn. 0 = never measured."""
    out = {"prompt": 0.0, "gen": 0.0, "cached": 0}
    for m in reversed(chat.get("messages") or []):
        t = m.get("timings")
        if m.get("role") == "assistant" and isinstance(t, dict):
            out["prompt"] = float(t.get("prompt_per_second") or 0)
            out["gen"] = float(t.get("predicted_per_second") or 0)
            out["cached"] = int(t.get("cache_n") or 0)
            break
    return out


_THINK_RE = None


def _strip_think(text: str) -> str:
    import re
    global _THINK_RE
    if _THINK_RE is None:
        _THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)
    return _THINK_RE.sub("", str(text or "")).strip()


def _gen_once(chat_id: str, ep: dict, messages: list[dict],
              cancel: threading.Event, on_progress=None,
              err_box: dict | None = None) -> str:
    """One plain streamed generation (no tools), accumulated. Registered
    in _streams so stop() can cut it. on_progress(chars_so_far) fires per
    chunk - the caller turns it into a visible liveness signal.

    err_box (when given) receives err_box['error'] = <reason> if the
    stream broke or the server sent an SSE error chunk - swallowing those
    silently is how compaction used to report 'came back empty' for what
    was really a server error."""
    body = {"model": ep["model"], "messages": messages, "stream": True}
    abort_box: dict = {}
    with _lock:
        _streams[chat_id] = _PreStream(abort_box)
    try:
        resp = providers.request(
            ep["provider"], "POST", "/v1/chat/completions",
            json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json"},
            timeout=STREAM_IDLE_TIMEOUT, abort_box=abort_box)
    except BaseException:
        with _lock:
            _streams.pop(chat_id, None)
        raise
    with _lock:
        _streams[chat_id] = resp
    out = []
    total = 0
    try:
        try:
            for chunk in _sse(resp):
                if cancel.is_set():
                    break
                if chunk.get("error") and err_box is not None:
                    e = chunk["error"]
                    err_box["error"] = str(e.get("message") or e) \
                        if isinstance(e, dict) else str(e)
                for ch in chunk.get("choices") or []:
                    txt = (ch.get("delta") or {}).get("content")
                    if txt:
                        out.append(txt)
                        total += len(txt)
                        if on_progress:
                            try:
                                on_progress(total)
                            except Exception:
                                pass
        except STREAM_ERRORS as e:
            # aborted/broken stream - return whatever accumulated, but
            # tell the caller (a cancel-induced break is not an error)
            if err_box is not None and not cancel.is_set():
                err_box["error"] = f"stream broke: {type(e).__name__}: {e}"
    finally:
        with _lock:
            if _streams.get(chat_id) is resp:
                _streams.pop(chat_id, None)
        try:
            resp.close()
        except Exception:
            pass
    return _strip_think("".join(out))


# the compaction REQUEST must itself fit the window, with room left to
# generate the summary - at least this many tokens (or nctx/8) stay free
COMPACT_RESERVE_TOKENS = 2048
COMPACT_TOOL_TRIM = 2_000       # chars kept per tool result in the request
COMPACT_KEEP_LAST = 4           # newest live messages never dropped


def _msg_est(m: dict) -> int:
    """chars/4 estimate for one wire message, tool_calls included."""
    content = m.get("content")
    if isinstance(content, list):   # multimodal user message: text parts
        n = sum(_est(p.get("text")) for p in content
                if isinstance(p, dict) and p.get("type") == "text")
    else:
        n = _est(content)
    for tc in m.get("tool_calls") or []:
        n += _est(json.dumps(tc))
    return n + 8


def _compact_request(root: Path, cfg: dict, chat_doc: dict,
                     nctx: int) -> tuple[list[dict], int]:
    """(messages for the summarization call, count dropped). At a FULL
    window the naive request (whole wire + prompt) exceeds nctx and the
    server errors or stalls - the exact 'hit compact at 100% and nothing
    happened' failure. So the request is BUDGETED: big tool results are
    trimmed, then the oldest messages are dropped until it fits with
    generation room to spare."""
    prompt = _read_prompt(root,
                          (cfg.get("chat") or {}).get("compaction_prompt")
                          or "prompts/compaction.md",
                          library.DEFAULT_COMPACTION_PROMPT)
    msgs = _wire_messages(root, cfg, chat_doc)
    msgs.append({"role": "user", "content": prompt})
    if not nctx:
        return msgs, 0
    # the floor matters: on a small window (-c 2048) the naive
    # nctx - RESERVE goes to zero or below, and an impossible budget
    # would shred the whole history into a garbage summary
    budget = max(nctx - max(COMPACT_RESERVE_TOKENS, nctx // 8), nctx // 2)

    def total():
        return sum(_msg_est(m) for m in msgs)

    # pass 1: giant tool results tell the summary nothing a trimmed one
    # doesn't - keep head + tail
    if total() > budget:
        for m in msgs:
            c = m.get("content")
            if m.get("role") == "tool" and isinstance(c, str) \
                    and len(c) > COMPACT_TOOL_TRIM:
                keep = COMPACT_TOOL_TRIM // 2
                m["content"] = (c[:keep] + "\n[... trimmed for compaction ...]\n"
                                + c[-keep:])

    # pass 2: drop the oldest messages (after the system prompt) until the
    # request fits - the newest COMPACT_KEEP_LAST plus the prompt survive
    dropped = 0
    while total() > budget and len(msgs) > COMPACT_KEEP_LAST + 2:
        msgs.pop(1)
        dropped += 1
        # never leave an orphan tool result at the front (strict chat
        # templates reject a tool message with no preceding call)
        while len(msgs) > 2 and msgs[1].get("role") == "tool":
            msgs.pop(1)
            dropped += 1

    # pass 3 (single huge messages): halve the longest content until it
    # fits - converges fast, and a truncated transcript still summarizes
    for _ in range(24):
        if total() <= budget:
            break
        big = max(msgs[1:], key=_msg_est)
        c = big.get("content")
        if not isinstance(c, str) or len(c) < 400:
            break
        big["content"] = c[:len(c) // 2] + "\n[... trimmed for compaction ...]"

    if dropped:
        msgs.insert(1, {"role": "user", "content":
                        f"[Note: the {dropped} oldest messages did not fit "
                        "in this summarization request and were omitted.]"})
    return msgs, dropped


def compact(root: Path, cfg: dict, chat_doc: dict, ep: dict,
            cancel: threading.Event, ev) -> bool:
    """Summarize the live context into a compaction marker. The original
    messages stay in the file (history keeps rendering); the wire carries
    the summary + everything after it."""
    _old, live = _active_slice(chat_doc)
    if not live:
        return False
    nctx = _nctx_for(cfg, chat_doc)
    tokens_before = context_breakdown(root, cfg, chat_doc)["usedTokens"]
    msgs, dropped = _compact_request(root, cfg, chat_doc, nctx)
    ev("compact_start", messages=len(live))
    # visible liveness: the page shows this count rising - if it stops,
    # the user KNOWS compaction stalled instead of wondering
    last_tick = {"t": 0.0}

    def prog(chars):
        now = time.monotonic()
        if now - last_tick["t"] >= 0.25:
            last_tick["t"] = now
            ev("compact_tick", tokens=(chars + 3) // 4)
    err_box: dict = {}
    t0 = time.monotonic()
    try:
        summary = _gen_once(str(chat_doc["id"]), ep, msgs, cancel,
                            on_progress=prog, err_box=err_box)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as e:
        if cancel.is_set():
            # the user's stop tore the request down - that's a cancel
            ev("compact_cancelled")
            return False
        detail = ""
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = ": " + e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
        ev("compact_error", msg=f"compaction failed: {e}{detail}")
        return False
    if cancel.is_set():
        ev("compact_cancelled")
        return False
    if err_box.get("error"):
        # a summary cut off mid-stream silently loses history - refuse it
        ev("compact_error", msg="compaction failed: " + str(err_box["error"]))
        return False
    if not summary:
        ev("compact_error", msg="compaction failed: the model returned an "
                                "empty summary - try again, or pick a "
                                "different model for this chat")
        return False
    chat_doc["messages"].append({"role": "compact", "content": summary,
                                 "replaced": len(live), "omitted": dropped,
                                 "tokensBefore": tokens_before,
                                 "durMs": int((time.monotonic() - t0) * 1000),
                                 "ts": int(time.time() * 1000)})
    chats.save_chat(root, chat_doc)
    ev("compact_done", replaced=len(live))
    return True


def _maybe_autocompact(root: Path, cfg: dict, chat_doc: dict, ep: dict,
                       cancel: threading.Event, ev) -> bool | None:
    """None = not needed; True/False = compaction ran and succeeded/failed.

    Two triggers, either fires:
    - the classic threshold (default 80% of the window);
    - PREDICTIVE: recent turns' real token growth (avg/worst over the last
      TURN_WINDOW turns) says the next turn could crash into the window -
      so a run of unusually fat turns compacts EARLY instead of shocking
      the chat into a wedged full-context state."""
    comp = (cfg.get("chat") or {}).get("compaction") or {}
    if not comp.get("auto", True):
        return None
    nctx = _nctx_for(cfg, chat_doc)
    if not nctx:
        return None
    used = context_breakdown(root, cfg, chat_doc)["usedTokens"]
    if used >= float(comp.get("threshold", 0.8)) * nctx \
            or used + _headroom(chat_doc, nctx) >= nctx:
        return compact(root, cfg, chat_doc, ep, cancel, ev)
    return None


def start_compaction(root: Path, chat_id: str, push) -> None:
    """Manual 'Compact now'. Registers in _running so it counts as
    inference (send is blocked, closing asks, quit gates see it)."""
    with _lock:
        rec = _running.get(chat_id)
        if rec and rec["thread"].is_alive():
            raise chats.ChatError("wait for the current response to finish")
        cancel = threading.Event()
        t = threading.Thread(target=_compact_worker,
                             args=(root, chat_id, cancel, push),
                             daemon=True, name=f"compact-{chat_id}")
        _running[chat_id] = {"cancel": cancel, "thread": t}
    t.start()


def _compact_worker(root: Path, chat_id: str, cancel: threading.Event,
                    push) -> None:
    ev = lambda kind, **f: _push(push, chat_id, kind, **f)
    try:
        cfg = libconfig.load(root)
        chat_doc = chats.load_chat(root, chat_id)
        ep = resolve_endpoint(cfg, chat_doc, probe=True)
        compact(root, cfg, chat_doc, ep, cancel, ev)
    except (chats.ChatError, libconfig.ConfigError,
            providers.ProviderError) as e:
        ev("compact_error", msg=str(e))
    except Exception as e:
        traceback.print_exc()
        ev("compact_error", msg=f"{type(e).__name__}: {e}")
    finally:
        with _lock:
            _running.pop(chat_id, None)
            _streams.pop(chat_id, None)


# --------------------------------------------------------------------------
# the loop

# a turn that "finishes" without an actual answer (thinking-only halt,
# empty stream) is passed back to the model this many times before the
# loop gives up
EMPTY_RESPONSE_RETRIES = 3


def _response_complete(chat: dict) -> bool:
    """Did the conversation actually END on a model answer? A turn whose
    last word is a bare thought, an empty message, or no message at all
    did not answer anything."""
    msgs = chat.get("messages") or []
    if not msgs:
        return False
    m = msgs[-1]
    return m.get("role") == "assistant" \
        and bool(str(m.get("content") or "").strip())


def send(root: Path, chat_id: str, push) -> None:
    """Start (or refuse) a worker for the chat's latest state. The user
    message must already be appended and saved by the caller."""
    with _lock:
        rec = _running.get(chat_id)
        if rec and rec["thread"].is_alive():
            raise chats.ChatError("a response is already streaming")
        cancel = threading.Event()
        t = threading.Thread(target=_worker,
                             args=(root, chat_id, cancel, push),
                             daemon=True, name=f"chat-{chat_id}")
        _running[chat_id] = {"cancel": cancel, "thread": t}
    t.start()


def _push(push, chat_id: str, kind: str, **fields):
    try:
        push({"type": "chat", "chatId": chat_id, "kind": kind, **fields})
    except Exception:
        pass


def _worker(root: Path, chat_id: str, cancel: threading.Event, push) -> None:
    ev = lambda kind, **f: _push(push, chat_id, kind, **f)
    try:
        cfg = libconfig.load(root)
        chat = chats.load_chat(root, chat_id)
        ep = resolve_endpoint(cfg, chat, probe=True)
        ev("start", model=ep["name"])
        # auto-compaction: before the first turn, AND between tool-loop
        # turns - a long agentic run grows the context mid-send, and only
        # checking once per send let it blow straight past the threshold.
        # One failure stops retrying for this send (no error-event spam).
        compact_ok = _maybe_autocompact(root, cfg, chat, ep, cancel, ev)
        retries = 0
        gave_up = False
        while True:
            if cancel.is_set():
                break
            done = _turn(root, cfg, chat, ep, cancel, ev)
            _save_from_loop(root, chat)
            if cancel.is_set():
                break
            if done:
                # a "done" turn that produced no actual ANSWER (a huge
                # thought that halted before the reply, or an empty
                # stream) is not an ending - pass the turn back to the
                # model, up to EMPTY_RESPONSE_RETRIES times. Past that,
                # give up honestly (the frontend words it accordingly).
                if _response_complete(chat):
                    break
                if retries >= EMPTY_RESPONSE_RETRIES:
                    gave_up = True
                    break
                retries += 1
                ev("retry", attempt=retries, max=EMPTY_RESPONSE_RETRIES)
                continue
            if compact_ok is not False:
                compact_ok = _maybe_autocompact(root, cfg, chat, ep,
                                                cancel, ev)
        ev("done", cancelled=cancel.is_set(), gaveUp=gave_up)
        # titling happens AFTER done, on its own thread - it must not block
        # the chat becoming usable again
        threading.Thread(target=_autotitle,
                         args=(root, cfg, str(chat["id"]), ep, push),
                         daemon=True, name=f"title-{chat_id}").start()
    except (chats.ChatError, libconfig.ConfigError, containers.ContainerError,
            providers.ProviderError) as e:
        ev("error", msg=str(e))
    except Exception as e:
        traceback.print_exc()
        ev("error", msg=f"{type(e).__name__}: {e}")
    finally:
        with _lock:
            _running.pop(chat_id, None)
            _streams.pop(chat_id, None)


def _save_from_loop(root: Path, chat: dict) -> None:
    """The loop's save: re-merge the user-editable knobs from disk FIRST,
    so a mode/network/env change made mid-conversation is never clobbered
    by the loop's stale in-memory copy."""
    _refresh_user_fields(root, chat)
    chats.save_chat(root, chat)


def _refresh_user_fields(root: Path, chat: dict) -> None:
    """Pull the user-editable knobs (model, permission mode, network,
    folders, container, env, title) from disk into the loop's in-memory
    doc. The user flips these MID-CONVERSATION; the loop must both honor
    the change on its very next decision and not clobber it with a stale
    copy on its next save."""
    try:
        disk = chats.load_chat(root, str(chat["id"]))
    except chats.ChatError:
        return
    for k in ("model", "permMode", "network", "folders", "container",
              "env", "title", "archived", "provider", "artifactsDismissed",
              "timeTravelMs", "artifactsOff", "thoughtTruncation",
              "timeSignalsOff", "knowledgeOff", "mcpPerms", "envHidden"):
        if k in disk:
            chat[k] = disk[k]
        else:
            chat.pop(k, None)


def _turn(root: Path, cfg: dict, chat: dict, ep: dict,
          cancel: threading.Event, ev) -> bool:
    """One streamed model turn. Returns True when the conversation is done
    (no tool calls)."""
    _refresh_user_fields(root, chat)
    body = {
        "model": ep["model"],
        "messages": _wire_messages(root, cfg, chat),
        "stream": True,
        "tools": tool_specs(cfg, str(chat.get("permMode") or ""),
                            network=containers.net_mode(chat.get("network")),
                            artifacts=not chat.get("artifactsOff"),
                            knowledge=not chat.get("knowledgeOff"),
                            mcp_perms=chat.get("mcpPerms")),
        # llama.cpp-style extensions (ninfer speaks them too):
        # per-chunk timings → LIVE tok/s; prompt_progress chunks → a real
        # prompt-processing progress bar instead of a silent stall
        "timings_per_token": True,
        "return_progress": True,
        "stream_options": {"include_usage": True},
        # reuse the KV cache for the unchanged prompt prefix - explicit,
        # so older llama-server builds behave like new ones
        "cache_prompt": True,
    }
    _apply_reasoning(body, store.reasoning_get(
        str(root), providers.model_key(ep["provider"]["name"], ep["model"])))
    t0 = time.monotonic()
    ttft = None
    # a cancel must be able to abort the request even while the server is
    # still chewing the prompt (no headers yet) - register the abortable
    # connection BEFORE sending
    abort_box: dict = {}
    with _lock:
        _streams[chat["id"]] = _PreStream(abort_box)
    try:
        resp = providers.request(
            ep["provider"], "POST", "/v1/chat/completions",
            json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json"},
            timeout=STREAM_IDLE_TIMEOUT, abort_box=abort_box)
    except urllib.error.HTTPError as e:
        with _lock:
            _streams.pop(chat["id"], None)
        try:
            detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            detail = ""
        raise chats.ChatError(f"the server answered HTTP {e.code}: {detail}")
    except STREAM_ERRORS as e:
        with _lock:
            _streams.pop(chat["id"], None)
        if cancel.is_set():
            return True   # the user's abort tore the request down - quiet
        reason = getattr(e, "reason", None) or e
        raise chats.ChatError(f"cannot reach {ep['name']}: {reason}")

    with _lock:
        _streams[chat["id"]] = resp

    content, reasoning = [], []
    calls: dict[int, dict] = {}
    timings = usage = None
    stream_err = None
    last_live = 0.0   # throttle for live_stats pushes
    try:
        for chunk in _sse(resp):
            if cancel.is_set():
                break
            if chunk.get("timings"):
                timings = chunk["timings"]
                # live speed readout - at most ~3/s so the bus stays light
                now = time.monotonic()
                if now - last_live >= 0.3:
                    last_live = now
                    ev("live_stats", timings=timings)
            if chunk.get("prompt_progress"):
                # the server is chewing the prompt: total/cache/processed/
                # time_ms - the page renders a real progress bar + ETA
                ev("progress", progress=chunk["prompt_progress"])
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                txt = delta.get("content")
                if txt:
                    if ttft is None:
                        ttft = time.monotonic() - t0
                    content.append(txt)
                    ev("delta", text=txt)
                thk = delta.get("reasoning_content")
                if thk:
                    if ttft is None:
                        ttft = time.monotonic() - t0
                    reasoning.append(thk)
                    ev("think", text=thk)
                for tc in delta.get("tool_calls") or []:
                    i = int(tc.get("index") or 0)
                    slot = calls.setdefault(i, {"id": tc.get("id") or f"call_{i}",
                                                "name": "", "args": ""})
                    fn = tc.get("function") or {}
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                        ev("tool_begin", callId=slot["id"], tool=slot["name"])
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
    except STREAM_ERRORS as e:
        # keep whatever streamed before the break - the partial answer is
        # appended (flagged stopped) so Continue can pick it back up. A
        # cancel-induced break (stop() closes the response under our
        # feet) is silent by design.
        if not cancel.is_set():
            stream_err = str(e)
    finally:
        with _lock:
            _streams.pop(chat["id"], None)
        try:
            resp.close()
        except Exception:
            pass

    if timings or usage or ttft is not None:
        ev("stats", timings=timings, usage=usage,
           ttftMs=int((ttft or 0) * 1000))

    text = _strip_echoed_stamp("".join(content))
    think = "".join(reasoning)
    msg = {"role": "assistant", "content": text,
           "ts": int(time.time() * 1000)}
    _tt = int(chat.get("timeTravelMs") or 0)
    if _tt:
        # time travel armed: the reply SIGNALS the shifted time too
        msg["signalTs"] = msg["ts"] + _tt
    if cancel.is_set() or stream_err:
        msg["stopped"] = True    # abrupt end - the UI offers Continue
    if think:
        msg["thinking"] = think
    if timings:
        msg["timings"] = timings
    if usage:
        msg["usage"] = usage
    if ttft is not None:
        # persisted so diagnostics can show real time-to-first-token per
        # turn, not just for the turn that happens to be on screen
        msg["ttftMs"] = int(ttft * 1000)
    ordered = [calls[i] for i in sorted(calls)]
    if ordered:
        msg["tool_calls"] = [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
            for c in ordered]
    if text or think or ordered:
        chat["messages"].append(msg)
    # persist NOW: the frontend re-pulls the chat on tool events, and the
    # assistant turn (with its tool_calls) must already be on disk
    _save_from_loop(root, chat)
    if stream_err:
        raise chats.ChatError(f"stream broke: {stream_err}")
    if not ordered or cancel.is_set():
        return True

    for c in ordered:
        _run_tool(root, cfg, chat, c, cancel, ev)
        _save_from_loop(root, chat)
        if cancel.is_set():
            return True
    return False


def _run_tool(root: Path, cfg: dict, chat: dict, call: dict,
              cancel: threading.Event, ev) -> None:
    # the user may have flipped the permission mode (or network, env,
    # container…) while the previous call streamed or waited - decide
    # THIS call under the settings as they are now
    _refresh_user_fields(root, chat)
    name, call_id = call["name"], call["id"]
    try:
        args = json.loads(call["args"] or "{}")
        if not isinstance(args, dict):
            args = {}
    except ValueError:
        args = {"_raw": call["args"]}
    perm = perm_for(cfg, name, str(chat.get("permMode") or ""),
                    chat.get("mcpPerms"))
    ev("tool_call", callId=call_id, tool=name, args=args, perm=perm)

    # three outcomes: ok / failed / cancelled. `ok` stays the boolean the
    # wire and history always carried; `cancelled` rides alongside so the
    # UI can tell "the user stopped this" from "this went wrong".
    result_ok = True
    cancelled = False
    ran = False   # _exec_tool actually returned (side effects may exist)
    if perm in ("deny", "disabled"):
        # 'disabled' tools are never OFFERED, but a model can still call
        # one by name (stale context, or the level flipped mid-stream) -
        # the most restrictive level must never fall through to allow
        result_ok = False
        result = ("denied by loom.yaml permissions" if perm == "deny"
                  else "this tool is disabled in the active permission mode")
    else:
        if perm == "ask":
            GATE.prepare(chat["id"], call_id, name)
            ev("tool_wait", callId=call_id)
            decision = GATE.wait(call_id, cancel)
            if decision != "allow":
                result_ok = False
                result = "the user declined this tool call"
        if result_ok:
            ev("tool_exec", callId=call_id)
            try:
                result = _exec_tool(
                    root, cfg, chat, name, args, cancel,
                    on_output=lambda s: ev("tool_output", callId=call_id, text=s))
                ran = True
                # a tool may return (text, status) - the shell does, so a
                # nonzero exit / timeout / cancel shows as what it IS
                if isinstance(result, tuple):
                    result, status = result
                    if status == "cancelled":
                        result_ok = False
                        cancelled = True
                    elif status != "ok":
                        result_ok = False
            except (chats.ChatError, containers.ContainerError,
                    library.LibraryError) as e:
                result_ok = False
                result = f"error: {e}"
            except Exception as e:
                traceback.print_exc()
                result_ok = False
                result = f"error: {type(e).__name__}: {e}"
    result = str(result)[:MAX_TOOL_RESULT]
    ev("tool_result", callId=call_id, ok=result_ok, cancelled=cancelled,
       result=result[:4000])
    msg = {"role": "tool", "tool_call_id": call_id,
           "name": name, "content": result,
           "ok": result_ok, "ts": int(time.time() * 1000)}
    if cancelled:
        msg["cancelled"] = True
    chat["messages"].append(msg)
    # anything the tool left in /artifacts is announced to the user - a
    # command that FAILED or was cancelled may still have written files
    # before it ended, so this keys on "it ran", not on "it succeeded"
    if ran and name in ("shell", "write_file", "edit_file"):
        try:
            _sync_artifacts(root, chat, ev)
        except OSError:
            pass


# --------------------------------------------------------------------------
# reasoning preference → request shape

def _apply_reasoning(body: dict, pref: dict | None) -> None:
    """Translate a stored per-model reasoning preference into the request.
    No preference = touch nothing (the server's own default)."""
    if not pref:
        return
    method = pref.get("method")
    level = str(pref.get("level") or "")
    if method == "effort":
        # the OpenAI-style request field. llama-server forwards it into
        # the chat template (Qwen 3.8 grades on low/medium/xhigh; gpt-oss
        # on low/medium/high) and "none" disables reasoning outright -
        # our "off" maps to that
        body["reasoning_effort"] = "none" if level == "off" else level
    elif method == "template":
        # boolean Jinja template kwarg (Qwen3-style enable_thinking)
        body["chat_template_kwargs"] = {"enable_thinking": level == "on"}
    elif method == "prompt":
        # soft switch appended to the system prompt (/think, /no_think)
        for m in body.get("messages") or []:
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                m["content"] += "\n\n" + ("/think" if level != "off"
                                          else "/no_think")
                break


def _autotitle(root: Path, cfg: dict, chat_id: str, ep: dict,
               push) -> None:
    """Model-generated chat title, using the library's title prompt:
    specific for concrete chats, creative for vague ones, and steered away
    from existing titles. Falls back to first-words on any failure, and a
    uniqueness suffix guarantees no duplicates either way."""
    try:
        chat_doc = chats.load_chat(root, chat_id)
    except chats.ChatError:
        return
    if chat_doc.get("title") not in (None, "", "New chat"):
        return
    first = next((m for m in chat_doc["messages"]
                  if m.get("role") == "user"), None)
    if not first:
        return
    words = str(first.get("content") or "").split()
    fallback = " ".join(words[:8])[:60].strip() or "New chat"
    existing = [c["title"] for c in chats.list_chats(root)
                if c["id"] != chat_id and c.get("title")]
    title = ""
    try:
        tp = _read_prompt(root,
                          (cfg.get("chat") or {}).get("title_prompt")
                          or "prompts/title.md",
                          library.DEFAULT_TITLE_PROMPT)
        last_asst = next((m.get("content") or "" for m in
                          reversed(chat_doc["messages"])
                          if m.get("role") == "assistant"), "")
        ask = (tp + "\n\nExisting titles (do not duplicate):\n"
               + "\n".join(f"- {t}" for t in existing[:50])
               + "\n\n<conversation>\nUser: "
               + str(first.get("content") or "")[:1500]
               + "\nAssistant: " + str(last_asst)[:1500]
               + "\n</conversation>")
        out = _gen_once(chat_id, ep, [{"role": "user", "content": ask}],
                        threading.Event())
        title = out.splitlines()[0].strip().strip('"\'' + ".:;") if out else ""
        title = title[:60].strip()
    except Exception:
        title = ""
    if not title:
        title = fallback
    base, n = title, 2
    lower = {t.lower() for t in existing}
    while title.lower() in lower:
        title = f"{base} ({n})"
        n += 1
    try:
        chat_doc = chats.load_chat(root, chat_id)   # don't clobber newer state
        if chat_doc.get("title") in (None, "", "New chat"):
            chat_doc["title"] = title
            chats.save_chat(root, chat_doc)
            _push(push, chat_id, "title", title=title)
    except chats.ChatError:
        pass
