"""llama-server lifecycle — local and remote hosts, no listeners.

Every managed server runs as a `llama-server` process ON its host (an SSH
destination, or this machine when the record has no host) bound to a UNIX
SOCKET — no TCP port opens on any interface, anywhere. Loom reaches the
socket through sshtunnel (AF_UNIX locally, an ssh stdio bridge remotely)
and manages the process with short bash scripts over the same multiplexed
ssh connection.

IDENTITY: the state dir is keyed by the server RECORD's id — stable across
edits, so Edit → Restart lands on the same directory and destroying the
record removes exactly its state.

DEATH ON APP EXIT: each server is a child of a small bash SUPERVISOR held
open through sshtunnel.Hold — a pipe (tunneled over ssh for remote hosts)
whose EOF is the kill signal. The kernel closes that pipe the moment Loom
dies — cleanly, crashed, or SIGKILLed — and the supervisor TERM→KILLs the
server on the host. No orphans, no cooperation from the dying app required.
The supervisor also exits when the server dies for any OTHER reason (crash,
OOM), which is how Loom observes it: the Hold's on_exit fires and the UI
says so. A flock around check-or-spawn closes start-twice races.

STATE on the host:  ~/.loom/servers/<id>/
    s.sock   the unix socket. The ".sock" suffix is MANDATORY — it is what
             makes llama-server bind AF_UNIX instead of parsing the path as
             a hostname and dying at startup. Short leaf because AF_UNIX
             paths cap at ~108 chars; very long homes fall back to
             /tmp/loom-<uid>/<id>.sock
    pid      spawned process id
    cmd      the exact command line (identity check against /proc — a
             recycled pid never passes for our server)
    meta.json  model, args, when
    log      llama-server stdout+stderr
    lock     flock target

STATUS for the UI: every observation lands in an in-memory registry and is
pushed through on_status() → the frontend. States: stopped | starting |
loading | running | stopping | error; events carry a `level` so the
frontend can rank severity without guessing.
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
import urllib.error

from loom import sshtunnel

# generous by design: a big GGUF on a Vulkan box can spend minutes loading;
# /health tells us it is WORKING (503 loading) vs absent (connect refused)
LOAD_TIMEOUT_S = 600
SPAWN_POLL_S = 1.0
HTTP_TIMEOUT = 10

DEFAULT_SERVER_BIN = "llama-server"

# What a freshly defined server's context field starts at.
DEFAULT_CTX = 128000

# What a freshly defined server's flags editor starts with. Prefilled
# text, not hidden defaults: the user sees it, edits it, owns it. Model
# and context are NOT here — they have their own fields.
DEFAULT_FLAGS = """\
-ngl 99
-fa on
-kvu
-ctk q4_0 -ctv q4_0
--spec-type draft-mtp
--spec-draft-n-max 2
--spec-draft-n-min 0
--spec-draft-p-min 0.75
-np 1
"""


# user-facing lines collapse home dirs to ~ (works for remote homes too —
# the pattern, not this process's $HOME, decides)
_TILDE_RE = re.compile(r"(^|[\s='\"(])/(?:home|Users)/[^/\s]+")


def tilde(s: str) -> str:
    return _TILDE_RE.sub(lambda m: m.group(1) + "~", str(s))


class SrvError(Exception):
    pass


def split_flags(text: str) -> list[str]:
    """The user's flags text → argv tokens. shlex rules: newlines are
    whitespace, quoting works, # starts a comment."""
    try:
        return shlex.split(str(text or ""), comments=True)
    except ValueError as e:
        raise SrvError(f"llama-server flags don't parse: {e}")


def compose_args(rec: dict) -> list[str]:
    """The llama-server argv tail (everything after --host <sock>).

    Loom composes what identifies the server — the model file, its alias,
    and the context size — and passes the user's flags VERBATIM, in the
    user's order. --jinja comes last (tool calling REQUIRES the jinja
    template engine; without it an agent silently never calls a tool),
    unless the user's flags say --embedding — an embedding server has no
    chat template to render."""
    model = str(rec.get("model") or "").strip()
    if not model:
        raise SrvError("the server has no model file set")
    args = ["-m", model]
    mmproj = str(rec.get("mmproj") or "").strip()
    if mmproj:
        args += ["--mmproj", mmproj]
    name = str(rec.get("name") or "").strip()
    if name:
        args += ["--alias", name]
    if rec.get("ctx") is not None and str(rec.get("ctx")) != "":
        try:
            args += ["-c", str(max(0, int(rec["ctx"])))]
        except (TypeError, ValueError):
            raise SrvError(f"context size is not a number: {rec['ctx']!r}")
    flags = split_flags(rec.get("flags") or "")
    args += flags
    if "--embedding" not in flags and "--embeddings" not in flags:
        args.append("--jinja")
    return args


# --------------------------------------------------------------------------
# in-memory status registry → the frontend

_lock = threading.Lock()
_servers: dict[str, dict] = {}     # record id -> status record
_sups: dict[str, "sshtunnel.Hold"] = {}   # record id -> supervisor lifeline
_shutting_down = False
_status_cb = None


def on_status(cb) -> None:
    global _status_cb
    _status_cb = cb


def _push_status(event: dict | None = None) -> None:
    cb = _status_cb
    if cb is None:
        return
    try:
        cb({"servers": snapshot(), "event": event})
    except Exception:
        pass   # the UI feed is best-effort; lifecycle must never die for it


def snapshot() -> list[dict]:
    with _lock:
        return [dict(v) for v in _servers.values()]


def _set_state(sid: str, state: str, level: str = "info", detail: str = "",
               **fields) -> None:
    """One observation about one server. state: stopped | starting | loading
    | running | stopping | error. level: the indicator severity the frontend
    ranks (error > warning > transition > stopping > info)."""
    with _lock:
        rec = _servers.setdefault(sid, {"id": sid})
        rec.update(fields)
        rec["state"] = state
        rec["detail"] = detail
        rec["ts"] = int(time.time() * 1000)
    _push_status({"id": sid, "state": state, "level": level, "detail": detail})


def forget(sid: str) -> None:
    with _lock:
        _servers.pop(sid, None)
    _push_status(None)


def running_count() -> int:
    with _lock:
        return sum(1 for r in _servers.values()
                   if r.get("state") in ("running", "loading", "starting"))


# --------------------------------------------------------------------------
# host-side scripts (bash over sshtunnel.run) — each prints ONE json line

def _dir_prelude(sid: str) -> str:
    # LOOM_HOME is honored locally (tests point it at a temp dir); remote
    # hosts use the default location.
    # The socket MUST end in ".sock": that suffix is llama-server's trigger
    # to bind AF_UNIX at all — any other name is parsed as an IP/hostname
    # and the server exits at startup.
    return (
        'ROOT="${LOOM_HOME:-$HOME/.loom}/servers"\n'
        f'K={shlex.quote(sid)}\n'
        'D="$ROOT/$K"\n'
        'mkdir -p "$D" && chmod 700 "$ROOT" "$D"\n'
        'S="$D/s.sock"\n'
        # AF_UNIX path cap (~108): very deep homes fall back to a private
        # /tmp dir; the chosen path is always reported back in the JSON
        'if [ "${#S}" -gt 100 ]; then\n'
        '  T="/tmp/loom-$(id -u)"; mkdir -p "$T" && chmod 700 "$T"\n'
        '  S="$T/$K.sock"\n'
        'fi\n'
    )


_CHECK_SNIPPET = (
    # is the recorded pid alive AND still our command? (pid reuse guard:
    # the cmdline must mention our socket path)
    'alive=0\n'
    'if [ -f "$D/pid" ]; then\n'
    '  P=$(cat "$D/pid" 2>/dev/null)\n'
    '  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && '
    'tr "\\0" " " < "/proc/$P/cmdline" 2>/dev/null | grep -qF -- "$S"; then\n'
    '    alive=1\n'
    '  fi\n'
    'fi\n'
)


def _q1(a: str) -> str:
    """Quote one argv token for the host-side bash script. A leading ~/
    becomes "$HOME"/... so it expands ON THE HOST that runs the server —
    a shell expands ~ before llama-server ever sees it, but our quoted
    script args skip the shell's expansion, so we reproduce it explicitly
    (works identically for local and ssh hosts)."""
    a = str(a)
    if a == "~":
        return '"$HOME"'
    if a.startswith("~/"):
        return '"$HOME"' + shlex.quote(a[1:])
    return shlex.quote(a)


def _q(args: list[str]) -> str:
    return " ".join(_q1(a) for a in args)


def _run_json(host: str, script: str, timeout: float = 30) -> dict:
    try:
        rc, out, err = sshtunnel.run(host, script, timeout=timeout)
    except sshtunnel.RunError as e:
        raise SrvError(f"cannot reach {host or 'this machine'}: {e}")
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        obj = json.loads(line)
    except ValueError:
        raise SrvError(
            f"host {host or 'local'} answered unexpectedly "
            f"(rc {rc}): {(err or out or 'no output').strip()[:300]}")
    return obj


def _hold_json(host: str, script: str, timeout: float = 60) -> tuple[dict, "sshtunnel.Hold"]:
    """Run a script through a Hold (stdin kept open) and parse its first
    line of JSON. The caller decides the Hold's fate: register it as a
    supervisor lifeline, or release() it for the short-script outcomes."""
    try:
        hold = sshtunnel.Hold(host, script)
    except sshtunnel.HoldError as e:
        raise SrvError(f"cannot reach {host or 'this machine'}: {e}")
    try:
        line = hold.line(timeout=timeout)
    except sshtunnel.HoldError as e:
        hold.close()
        raise SrvError(f"cannot reach {host or 'this machine'}: {e}")
    try:
        obj = json.loads((line or "").strip())
    except ValueError:
        hint = hold.error_hint()
        hold.close()
        raise SrvError(
            f"host {host or 'local'} answered unexpectedly: "
            f"{(line or hint or 'no output').strip()[:300]}")
    return obj, hold


def http_to(host: str, sock: str, method: str, path: str, body: bytes | None = None,
            headers: dict | None = None, timeout: float = HTTP_TIMEOUT):
    """HTTP over the server's unix socket (never a TCP port). Returns the
    raw response; the caller owns close()."""
    return sshtunnel.request(method, f"http://llama{path}", headers, body,
                             timeout, host, unix=sock)


def _http_json(host: str, sock: str, path: str, timeout: float = HTTP_TIMEOUT) -> dict:
    resp = http_to(host, sock, "GET", path, timeout=timeout)
    with resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# operations

def start(rec: dict, notice=None, load_timeout: float | None = None) -> dict:
    """Find-or-start the llama-server for a config record. Returns
    {id, sock, nCtx, started, props}. Blocks (with notice() progress)
    through model loading.

    NEVER call this on a pywebview bridge thread: bridge threads are
    non-daemon, and a long model load would block interpreter shutdown.
    Bridge endpoints hand the work to a daemon thread and report via the
    Bus."""
    sid = str(rec.get("id") or "")
    if not sid:
        raise SrvError("server record has no id")
    host = str(rec.get("host") or "")
    name = str(rec.get("name") or sid)
    server_bin = str(rec.get("serverBin") or "").strip() or DEFAULT_SERVER_BIN
    args = compose_args(rec)

    def note(msg):
        if notice:
            try:
                notice(tilde(msg))
            except Exception:
                pass

    _set_state(sid, "starting", level="transition", host=host, name=name,
               model=rec.get("model"), detail="checking for a running server")
    # The whole script is wrapped in { } — bash must parse (consume) ALL of
    # it from stdin before executing a line, because after the spawn this
    # same stdin becomes the LIFELINE: a background `cat` blocks on it, and
    # any script text still in the pipe would be eaten by that cat.
    script = (
        "{\n"
        "set -u\n"
        + _dir_prelude(sid)
        + 'exec 9>"$D/lock"\n'
        'flock -w 30 9 || { echo "{\\"state\\":\\"lockfail\\"}"; exit 0; }\n'
        + _CHECK_SNIPPET +
        'if [ "$alive" = 1 ]; then\n'
        '  printf \'{"state":"running","sock":"%s","pid":%s}\\n\' "$S" "$P"\n'
        '  exit 0\n'
        'fi\n'
        'rm -f "$S" "$D/pid"\n'
        'command -v ' + _q1(server_bin) + ' >/dev/null 2>&1 || '
        '{ echo "{\\"state\\":\\"nobinary\\"}"; exit 0; }\n'
        'CMD=' + shlex.quote(_q([server_bin, "--host"])) + '" $(printf %q "$S") "'
        + shlex.quote(_q(args)) + '\n'
        'printf "%s" "$CMD" > "$D/cmd"\n'
        'cat > "$D/meta.json" <<\'EOF\'\n'
        + json.dumps({"name": name, "model": rec.get("model"), "args": args,
                      "startedTs": int(time.time() * 1000)}) + "\n"
        'EOF\n'
        # the server is a CHILD of this supervisor — its life is bounded by
        # ours, and ours by the lifeline. 9>&- matters: the child must NOT
        # inherit the flock fd, or the lock would be held for its whole life
        + _q1(server_bin) + ' --host "$S" '
        + _q(args) + ' < /dev/null >> "$D/log" 2>&1 9>&- &\n'
        'SRV=$!\n'
        'echo "$SRV" > "$D/pid"\n'
        'exec 9>&-\n'
        'printf \'{"state":"spawned","sock":"%s","pid":%s}\\n\' "$S" "$SRV"\n'
        # the LIFELINE: stdin EOF = Loom is gone (crash included — the
        # kernel closes the pipe) → kill the server. A background command's
        # stdin is silently /dev/null in a non-interactive shell, so the
        # real stdin rides in on fd 8. The subshell's own stdio goes to
        # /dev/null so a lingering cat can never hold the ssh session open.
        'exec 8<&0\n'
        '( cat <&8 >/dev/null 2>&1\n'
        '  kill -TERM "$SRV" 2>/dev/null\n'
        '  for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$SRV" 2>/dev/null || break; sleep 0.5; done\n'
        '  kill -KILL "$SRV" 2>/dev/null ) >/dev/null 2>&1 &\n'
        'LIFE=$!\n'
        'exec 8<&-\n'
        # ssh session torn down (network cut, sshd exit) → same rule as
        # app death: take the server with us
        'trap \'kill -TERM "$SRV" 2>/dev/null\' HUP TERM INT\n'
        'wait "$SRV" 2>/dev/null\n'
        'wait "$SRV" 2>/dev/null\n'   # re-enter if a trap interrupted the first
        'kill "$LIFE" 2>/dev/null\n'
        # clean up under the lock, and only OUR files: by now a stop() may
        # have cleaned already, or a respawn may own the dir — never touch
        # the next server's socket/pid
        'exec 9>"$D/lock"\n'
        'flock -w 5 9 || exit 0\n'
        'if [ "$(cat "$D/pid" 2>/dev/null)" = "$SRV" ]; then rm -f "$S" "$D/pid"; fi\n'
        'exit 0\n'
        "}\n"
    )
    note(f"checking for a running server on {host or 'this machine'}")
    got, hold = _hold_json(host, script, timeout=60)
    state = got.get("state")
    if state == "spawned":
        # OUR supervisor: register it, and watch for its death — that is
        # how we learn the server is gone even when something else killed it
        hold.on_exit = lambda: _holder_exit(sid, hold)
        with _lock:
            old = _sups.get(sid)
            _sups[sid] = hold
        if old is not None:
            old.release()   # superseded (shouldn't happen; belt-and-braces)
        if not hold.alive():          # died before/while registering
            _holder_exit(sid, hold)
    else:
        hold.release()   # a short script; it has already said its piece
    if state == "lockfail":
        _set_state(sid, "error", level="error",
                   detail="another process holds the server lock (30s)")
        raise SrvError(f"{name}: the server lock on {host or 'this machine'} "
                       "stayed busy for 30s — try again")
    if state == "nobinary":
        _set_state(sid, "error", level="error",
                   detail=f"{server_bin} not found on {host or 'this machine'}")
        raise SrvError(
            f"{server_bin} is not installed on {host or 'this machine'} "
            "(install llama.cpp, or set the server binary path)")

    sock = got.get("sock") or ""
    started = state == "spawned"
    if started:
        _set_state(sid, "loading", level="transition", sock=sock,
                   pid=got.get("pid"), detail="loading model…")
        note(f"spawned llama-server (pid {got.get('pid')}) — socket {sock}")
    else:
        note(f"found a running server (pid {got.get('pid')}) — reusing {sock}")

    # wait for the socket + model load: connect-refused means still binding,
    # HTTP 503 means loading (that's WORK, not a hang), 200 means ready.
    # None = default; an EXPLICIT value is honored as given (`or` would
    # silently turn load_timeout=0 into the 600s default)
    budget = ((load_timeout if load_timeout is not None
               else LOAD_TIMEOUT_S) if started else 30)
    t_wait0 = time.monotonic()
    deadline = t_wait0 + budget
    last_err = "no answer yet"
    last_alive = time.monotonic()
    while True:
        try:
            _http_json(host, sock, "/health", timeout=HTTP_TIMEOUT)
            break
        except urllib.error.HTTPError as e:
            if e.code == 503:
                last_err = "loading model"
                elapsed = int(time.monotonic() - t_wait0) if started else 0
                _set_state(sid, "loading", level="transition", sock=sock,
                           detail=f"loading model… {elapsed}s" if started
                           else "loading model…")
                if elapsed < 1 or elapsed % 5 == 0:
                    note(f"{name}: loading model… {elapsed}s")
            else:
                last_err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = str(getattr(e, "reason", e))
            elapsed = int(time.monotonic() - t_wait0)
            if started and (elapsed < 1 or elapsed % 5 == 0):
                note(f"waiting for the server socket… {elapsed}s ({last_err})")
            # the socket isn't answering — is the process even alive? A
            # spawn that died (bad flag, OOM, unsupported model) must fail
            # NOW with its log, not after the whole load timeout
            if started and time.monotonic() - last_alive > 3:
                last_alive = time.monotonic()
                if not _pid_alive(host, sid):
                    died = _peek_log(host, sid)
                    _set_state(sid, "error", level="error", sock=sock,
                               detail="the server exited during startup")
                    note("✗ llama-server exited during startup")
                    raise SrvError(
                        f"{name}: llama-server exited during startup"
                        + (f" — log tail:\n{died}" if died
                           else " (its log is empty)"))
        if time.monotonic() > deadline:
            died = _peek_log(host, sid)
            _set_state(sid, "error", level="error", sock=sock,
                       detail=f"did not become ready: {last_err}")
            raise SrvError(
                f"{name} did not become ready ({last_err})"
                + (f" — server log tail:\n{died}" if died else ""))
        time.sleep(SPAWN_POLL_S)

    props = {}
    n_ctx = None
    try:
        props = _http_json(host, sock, "/props")
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
    except (SrvError, urllib.error.URLError, urllib.error.HTTPError,
            OSError, ValueError):
        pass   # health said ok; props is a nicety (context accuracy)
    _set_state(sid, "running", sock=sock, nCtx=n_ctx,
               detail=("started" if started else "already running"))
    if started:
        note(f"{name} is ready")
    return {"id": sid, "sock": sock, "nCtx": n_ctx, "started": started,
            "props": props, "host": host}


def _pid_alive(host: str, sid: str) -> bool:
    """Is the server's recorded pid alive AND still our command? One cheap
    roundtrip over the multiplexed connection."""
    script = ("set -u\n" + _dir_prelude(sid) + _CHECK_SNIPPET
              + 'printf \'{"alive":%s}\\n\' "$alive"')
    try:
        return str(_run_json(host, script, timeout=15).get("alive")) == "1"
    except SrvError:
        return True   # can't tell — keep waiting rather than false-alarm


def _peek_log(host: str, sid: str) -> str:
    """Last lines of the server log — the honest error when a spawn dies."""
    script = ("set -u\n" + _dir_prelude(sid)
              + 'printf \'{"log": %s}\\n\' "$(tail -c 2000 "$D/log" 2>/dev/null '
                '| python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" '
                '2>/dev/null || echo \'""\')"')
    try:
        return str(_run_json(host, script).get("log") or "").strip()[-1500:]
    except SrvError:
        return ""


def log_tail(host: str, sid: str, bytes_: int = 16000) -> str:
    """Log tail for the UI's log viewer."""
    n = max(1000, min(int(bytes_ or 16000), 200000))
    script = ("set -u\n" + _dir_prelude(sid)
              + f'printf \'{{"log": %s}}\\n\' "$(tail -c {n} "$D/log" 2>/dev/null '
                '| python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" '
                '2>/dev/null || echo \'""\')"')
    return str(_run_json(host, script).get("log") or "")


def log_clear(host: str, sid: str) -> None:
    """Truncate the server log — the viewer's Clear button. Same script
    channel as log_tail, so local and ssh hosts behave identically; the
    running server keeps appending to the truncated file."""
    script = ("set -u\n" + _dir_prelude(sid)
              + ': > "$D/log" 2>/dev/null; printf \'{"ok": 1}\\n\'')
    _run_json(host, script)


def _holder_exit(sid: str, hold) -> None:
    """OUR supervisor for `sid` ended — its server is gone. Our own stop()
    already reported; a death while RUNNING is a crash worth surfacing.
    Startup failures are start()'s poll loop's report, not ours."""
    with _lock:
        if _sups.get(sid) is not hold:
            return                     # superseded or already handled
        del _sups[sid]
        rec = dict(_servers.get(sid) or {})
        if _shutting_down:
            return
    if rec.get("state") in ("stopping", "stopped"):
        return                         # deliberate stop; already reported
    if rec.get("state") != "running":
        return   # startup failure — start()'s poll loop owns that report
    host = rec.get("host") or ""
    name = rec.get("name") or sid
    log = _peek_log(host, sid)
    _set_state(sid, "error", level="error",
               detail="server exited unexpectedly"
               + (f" — log tail:\n{log[-400:]}" if log else ""))


def shutdown(timeout: float = 8.0) -> None:
    """App exit: cut every lifeline; each supervisor kills its server on
    the host. A crash needs no call here — the kernel closing the pipes IS
    the mechanism; this just makes a clean quit equally prompt and waits
    briefly so TERM (not the eventual KILL) is what the servers get."""
    global _shutting_down
    with _lock:
        _shutting_down = True
        holds = list(_sups.values())
        _sups.clear()
    for h in holds:
        h.release()
    deadline = time.monotonic() + timeout
    for h in holds:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            h.proc.wait(left)
        except Exception:
            pass


def stop(host: str, sid: str) -> dict:
    """Stop a server: TERM→KILL its process and clean the socket/pid."""
    _set_state(sid, "stopping", level="stopping", detail="stopping…")
    with _lock:
        hold = _sups.pop(sid, None)
    script = (
        "set -u\n"
        + _dir_prelude(sid)
        + 'exec 9>"$D/lock"\n'
        'flock -w 10 9 || true\n'
        + _CHECK_SNIPPET +
        'if [ "$alive" = 1 ]; then\n'
        '  kill -TERM "$P" 2>/dev/null\n'
        '  for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$P" 2>/dev/null || break; sleep 0.5; done\n'
        '  kill -KILL "$P" 2>/dev/null\n'
        '  rm -f "$S" "$D/pid"\n'
        '  echo \'{"state":"stopped","was":"running"}\'\n'
        'else\n'
        '  rm -f "$S" "$D/pid"\n'
        '  echo \'{"state":"stopped","was":"dead"}\'\n'
        'fi\n'
    )
    got = _run_json(host, script, timeout=30)
    if hold is not None:
        hold.release()   # the supervisor sees its child die and winds down
    _set_state(sid, "stopped", level="stopping", detail="stopped")
    return got


def destroy_state(host: str, sid: str) -> None:
    """Remove a server's state dir on its host — the on-host half of
    destroying a record. Stops the process first if it is still alive."""
    try:
        stop(host, sid)
    except SrvError:
        pass   # unreachable host: the config record still goes; state stays
    script = (
        "set -u\n"
        + _dir_prelude(sid)
        + 'rm -rf "$D"; rm -f "$S"\n'
        'echo \'{"state":"removed"}\'\n'
    )
    try:
        _run_json(host, script)
    except SrvError:
        pass
    forget(sid)


def refresh(records: list[dict]) -> list[dict]:
    """Re-check every configured server against its host (pid + cmdline)
    and converge the in-memory registry on the truth. Unreachable hosts mark
    their servers with an error detail rather than lying 'stopped'."""
    by_host: dict[str, list[dict]] = {}
    for r in records:
        by_host.setdefault(str(r.get("host") or ""), []).append(r)
    for host, recs in by_host.items():
        for r in recs:
            sid = str(r.get("id") or "")
            if not sid:
                continue
            name = str(r.get("name") or sid)
            try:
                alive = _alive_probe(host, sid)
            except SrvError as e:
                with _lock:
                    cur = _servers.setdefault(sid, {"id": sid})
                    cur.update({"host": host, "name": name,
                                "model": r.get("model"),
                                "state": cur.get("state") or "stopped",
                                "detail": f"host unreachable: {e}",
                                "ts": int(time.time() * 1000)})
                continue
            with _lock:
                cur = _servers.setdefault(sid, {"id": sid})
                cur.update({"host": host, "name": name, "model": r.get("model")})
                if alive:
                    if cur.get("state") not in ("running", "loading", "stopping"):
                        cur["state"] = "running"
                        cur["detail"] = "running"
                elif cur.get("state") not in ("starting", "loading", "error"):
                    cur["state"] = "stopped"
                    cur.setdefault("detail", "not running")
                cur["ts"] = int(time.time() * 1000)
    # drop registry entries whose record is gone (destroyed elsewhere)
    ids = {str(r.get("id")) for r in records}
    with _lock:
        for sid in [k for k in _servers if k not in ids]:
            del _servers[sid]
    _push_status(None)
    return snapshot()


def _alive_probe(host: str, sid: str) -> bool:
    script = ("set -u\n" + _dir_prelude(sid) + _CHECK_SNIPPET
              + 'printf \'{"alive":%s,"sock":"%s"}\\n\' "$alive" "$S"')
    got = _run_json(host, script, timeout=15)
    alive = str(got.get("alive")) == "1"
    if alive:
        with _lock:
            _servers.setdefault(sid, {"id": sid})["sock"] = got.get("sock")
    return alive


def sock_of(sid: str) -> str:
    with _lock:
        return str((_servers.get(sid) or {}).get("sock") or "")


def state_of(sid: str) -> str:
    with _lock:
        return str((_servers.get(sid) or {}).get("state") or "stopped")


def probe_host(host: str, server_bin: str = "", model: str = "") -> dict:
    """Edit-modal 'Test': is the host reachable, is llama-server there, does
    the model file exist? One roundtrip, honest answers."""
    bin_ = str(server_bin or "").strip() or DEFAULT_SERVER_BIN
    script = (
        "set -u\n"
        'BIN=""\n'
        'command -v ' + _q1(bin_) + ' >/dev/null 2>&1 && BIN=1\n'
        'MODEL=""\n'
        + ('[ -f ' + _q1(str(model)) + ' ] && MODEL=1\n' if model else "")
        + 'printf \'{"reachable":true,"binary":"%s","model":"%s"}\\n\' "$BIN" "$MODEL"\n'
    )
    try:
        got = _run_json(host, script, timeout=25)
    except SrvError as e:
        return {"reachable": False, "error": str(e)}
    return {"reachable": True,
            "binary": got.get("binary") == "1",
            "model": (got.get("model") == "1") if model else None}
