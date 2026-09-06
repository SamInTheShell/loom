"""Interactive terminal sessions (Ctrl+T) - adapted from CodeTree.

One session per terminal tab: the user picks a container definition from
loom.yaml (and optionally folders to mount, view or write mode, same
/mnt/<name> layout as chats), then a real interactive bash runs on a host
PTY inside `<engine> run --rm -it`. Output streams to the page as raw
chunks over the bus ({type:"term", sid, kind:"data"|"line"|"snapshot"|
"ready"|"exit"|"error"}); input comes back through term_write.

The --rm container is a CHILD of this process holding the PTY: when Loom
dies the PTY closes, the shell gets HUP, and the container removes itself
- no orphans. A rolling output buffer replays on (re)attach so terminals
survive tab switches and close-to-tray. Networking is off unless the tab
enables it.
"""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

import uuid

from loom import containers, envs, libconfig, store

_SESS: dict[str, dict] = {}
_LOCK = threading.RLock()

_TERM_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
_BUF_CAP = 512_000        # rolling replay buffer per session (chars)


class TermError(Exception):
    pass


def _cname(sid: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "", str(sid))[:50]
    if not s:
        raise TermError(f"invalid terminal id: {sid!r}")
    return "loom-term-" + s


def _emit(push, sid: str, kind: str, **kw) -> None:
    try:
        push({"type": "term", "sid": sid, "kind": kind, **kw})
    except Exception:
        pass


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(int(cols or 80), 500))
    rows = max(5, min(int(rows or 24), 200))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _extra_mounts(knowledge: Path | None,
                  artifacts: Path | None) -> list[str]:
    """The chat-view mounts: /knowledge read-only, /artifacts read-write
    (same layout containers.run_shell gives the model's shell)."""
    vol: list[str] = []
    if knowledge is not None and knowledge.is_dir():
        vol += ["-v", f"{knowledge.resolve()}:/knowledge:ro"]
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=True)
        vol += ["-v", f"{artifacts.resolve()}:/artifacts:rw"]
    return vol


def alive(tab_id: str) -> bool:
    with _LOCK:
        sess = _SESS.get(str(tab_id))
    return bool(sess and sess["proc"].poll() is None)


def open_session(push, root: Path, tab_id: str, container: str,
                 folders: list[dict] | None = None, network="none",
                 cols: int = 120, rows: int = 32,
                 env_name: str = "",
                 knowledge: Path | None = None,
                 artifacts: Path | None = None,
                 home: Path | None = None) -> None:
    """Build/pull the image if needed and start the interactive shell.
    Blocking (image builds take a while) - call on a worker thread;
    progress and errors arrive as term events."""
    sid = str(tab_id)
    with _LOCK:
        prev = _SESS.get(sid)
    carried = ""
    if prev is not None:
        with prev["lock"]:
            carried = prev["buf"]
    close_session(sid)

    # a fresh shell replaces whatever the page shows; carried history
    # replays above it
    _emit(push, sid, "snapshot", data=carried)
    try:
        cfg = libconfig.load(root)
        _emit(push, sid, "line", text="[loom] preparing container…")
        engine, image = containers.ensure_image_named(
            root, cfg, container,
            notice=lambda t: _emit(push, sid, "line", text=str(t)))
    except (libconfig.ConfigError, containers.ContainerError) as e:
        _emit(push, sid, "error", detail=str(e))
        return
    except Exception as e:
        _emit(push, sid, "error", detail=f"terminal setup failed: {e}")
        return

    try:
        vol, notes = containers.mounts_for(folders or [])
        vol += _extra_mounts(knowledge, artifacts)
        if knowledge is not None and knowledge.is_dir():
            notes.append("/knowledge (read-only) = " + str(knowledge))
        if artifacts is not None:
            notes.append("/artifacts (read-write) = " + str(artifacts))
        for n in notes:
            _emit(push, sid, "line", text="[loom] mounted " + n)
        if home is None:
            home = containers.chat_home("term-" + sid)
        name = _cname(sid)
        try:
            subprocess.run([engine, "rm", "-f", name], capture_output=True,
                           timeout=30)
        except Exception:
            pass   # a stale container that won't die surfaces at run below
        master, slave = pty.openpty()
        _set_winsize(master, cols, rows)
    except Exception as e:
        _emit(push, sid, "error", detail=f"terminal setup failed: {e}")
        return

    def _child_setup():
        # own session + the PTY as controlling terminal, so TIOCSWINSZ on
        # the master delivers SIGWINCH through the runtime into the tty
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env_args = []
    for k, v in _TERM_ENV.items():
        env_args += ["--env", f"{k}={v}"]
    # a named environment's vars ride a 0600 env-file - NEVER the argv,
    # where secrets would show in the host process list
    env_file = None
    if env_name:
        try:
            extra, missing = envs.resolve(root, str(env_name))
        except envs.EnvError as e:
            _emit(push, sid, "line", text=f"[loom] environment error: {e}")
            extra, missing = {}, []
        if extra:
            env_file = store.run_dir() / f"env-term-{uuid.uuid4().hex[:12]}"
            env_file.touch(mode=0o600)
            env_file.write_text("".join(f"{k}={v}\n"
                                        for k, v in extra.items()),
                                encoding="utf-8")
            env_args += ["--env-file", str(env_file)]
            _emit(push, sid, "line",
                  text="[loom] environment '" + str(env_name) + "' loaded: "
                       + ", ".join(sorted(extra)))
        if missing:
            _emit(push, sid, "line",
                  text="[loom] missing secrets (UNSET - set them in the "
                       "Environments tab): " + ", ".join(missing))
    argv = [engine, "run", "--rm", "-it", "--name", name,
            *containers.net_args(engine, network),
            "-v", f"{home}:/home/loom:rw",
            *vol, *containers._user_args(engine),
            "-e", "HOME=/home/loom", *env_args, "-w", "/home/loom",
            "--entrypoint", "/bin/bash", image, "-l"]

    def _drop_env_file():
        if env_file is not None:
            env_file.unlink(missing_ok=True)
    try:
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                preexec_fn=_child_setup, close_fds=True)
    except (OSError, subprocess.SubprocessError) as e:
        _drop_env_file()
        try:
            os.close(master)
        except OSError:
            pass
        try:
            os.close(slave)
        except OSError:
            pass
        _emit(push, sid, "error", detail=f"could not start the shell: {e}")
        return
    os.close(slave)
    # the runtime reads the env-file while starting up; give it a wide
    # margin, then the secrets file disappears from disk
    if env_file is not None:
        threading.Timer(30, _drop_env_file).start()

    sess = {"proc": proc, "fd": master, "engine": engine, "name": name,
            "push": push, "lock": threading.Lock(), "buf": carried}
    with _LOCK:
        _SESS[sid] = sess

    def _close_fd():
        # SINGLE-OWNER close: both the reader's epilogue and close_session
        # funnel through here. A double os.close on a recycled fd number
        # can close a Qt/Chromium descriptor and crash the whole app.
        with sess["lock"]:
            fd = sess.get("fd")
            sess["fd"] = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    sess["close_fd"] = _close_fd

    def record(text: str) -> None:
        with sess["lock"]:
            buf = sess["buf"] + text
            if len(buf) > _BUF_CAP:
                cut = len(buf) - _BUF_CAP
                # trim at an escape/newline boundary so a replay never
                # starts mid-sequence
                for cand in sorted(x for x in (buf.find("\x1b", cut),
                                               buf.find("\n", cut)) if x >= 0):
                    cut = cand
                    break
                buf = buf[cut:]
            sess["buf"] = buf

    def reader():
        # COALESCED streaming: one bus event per ~25ms window (or 128KB),
        # not one per PTY read - a firehose must not flood the JS bridge
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        pend, pend_len, last_flush, eof = [], 0, 0.0, False
        while not eof:
            try:
                r, _, _ = select.select([master], [], [],
                                        0.025 if pend else None)
            except OSError:
                break
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    eof = True
                    chunk = b""
                if not chunk:
                    eof = True
                text = dec.decode(chunk, eof)
                if text:
                    pend.append(text)
                    pend_len += len(text)
            now = time.monotonic()
            if pend and (not r or eof or pend_len >= 131072
                         or now - last_flush >= 0.025):
                data = "".join(pend)
                pend, pend_len, last_flush = [], 0, now
                record(data)
                _emit(push, sid, "data", data=data)
        code = proc.wait()
        with _LOCK:
            if _SESS.get(sid) is sess:
                del _SESS[sid]
        _close_fd()
        _emit(push, sid, "exit", code=code)

    threading.Thread(target=reader, daemon=True, name=f"term-{sid}").start()
    _emit(push, sid, "ready")


def attach(push, tab_id: str, cols: int, rows: int) -> bool:
    """Reconnect a page to a LIVE session: resize, replay the buffer as one
    snapshot, resume streaming. False = no live session (open fresh)."""
    sid = str(tab_id)
    with _LOCK:
        sess = _SESS.get(sid)
    if not sess or sess["proc"].poll() is not None:
        return False
    with sess["lock"]:
        fd = sess.get("fd")
    if fd is None:
        return False
    try:
        _set_winsize(fd, cols, rows)
    except OSError:
        return False
    with sess["lock"]:
        data = sess["buf"]
    _emit(push, sid, "snapshot", data=data)
    _emit(push, sid, "ready")
    return True


def write(tab_id: str, data: str) -> bool:
    """False = no live shell - the page reconnects and replays the input."""
    with _LOCK:
        sess = _SESS.get(str(tab_id))
    if not sess or sess["proc"].poll() is not None:
        return False
    payload = str(data).encode("utf-8", "replace")
    with sess["lock"]:
        fd = sess.get("fd")
    if fd is None:
        return False
    deadline = time.monotonic() + 2.0   # never block the JS bridge
    try:
        while payload:
            r = select.select([], [fd], [],
                              max(0.0, deadline - time.monotonic()))
            if not r[1]:
                return False
            n = os.write(fd, payload)
            payload = payload[n:]
        return True
    except OSError:
        return False


def resize(tab_id: str, cols: int, rows: int) -> bool:
    with _LOCK:
        sess = _SESS.get(str(tab_id))
    if not sess:
        return False
    with sess["lock"]:
        fd = sess.get("fd")
    if fd is None:
        return False
    try:
        _set_winsize(fd, cols, rows)
        return True
    except OSError:
        return False


def _infra_cmdline(cmd: str) -> bool:
    """Session plumbing vs the user's work - full command line, never the
    bare name (`sleep 86000` typed by the user is work)."""
    parts = cmd.split()
    if not parts:
        return True
    base = parts[0].rsplit("/", 1)[-1]
    rest = parts[1:]
    if base in ("bash", "sh") and rest in ([], ["-l"]):
        return True                       # the session's own shell
    if base in ("bash", "sh") and rest[:1] == ["-c"] and "/proc/" in cmd:
        return True                       # this probe
    return False


def procs(tab_id: str, timeout: int = 15) -> list[str]:
    """Processes in the tab's container BEYOND the shell - non-empty means
    closing kills real work. Never raises."""
    with _LOCK:
        sess = _SESS.get(str(tab_id))
    if not sess or sess["proc"].poll() is not None:
        return []
    try:
        out = subprocess.run(
            [sess["engine"], "exec", sess["name"], "bash", "-c",
             'for d in /proc/[0-9]*; do tr "\\0" " " < "$d/cmdline" '
             '2>/dev/null; echo; done'],
            capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return []
        found = {line.strip()[:40] for line in out.stdout.splitlines()
                 if line.strip() and not _infra_cmdline(line.strip())}
        return sorted(found)
    except Exception:
        return []


def live_count() -> int:
    with _LOCK:
        return sum(1 for s in _SESS.values() if s["proc"].poll() is None)


def close_session(tab_id: str) -> None:
    """Stop the shell; --rm removes the container."""
    with _LOCK:
        sess = _SESS.pop(str(tab_id), None)
    if not sess:
        return
    proc = sess["proc"]
    try:
        os.killpg(proc.pid, signal.SIGHUP)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    closer = sess.get("close_fd")
    if closer is not None:
        closer()
    subprocess.run([sess["engine"], "rm", "-f", sess["name"]],
                   capture_output=True, timeout=30)


def cleanup(tab_id: str) -> None:
    """Tab closed for good. Mounted folders are the user's - untouched."""
    close_session(str(tab_id))


def shutdown() -> None:
    with _LOCK:
        sids = list(_SESS)
    for sid in sids:
        close_session(sid)
