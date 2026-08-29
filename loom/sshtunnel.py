"""HTTP over SSH stdio forwarding — reach llama-servers on other machines
with NO local listener.

The classic `ssh -L localhost:1234:...` needs a real localhost port that
lingers, collides with other apps, and is usable by every local process.
Instead, each HTTP request here rides its own ssh stdio channel: a tiny
bridge on the far side (socat → nc -U → python3, whichever exists) connects
to the llama-server's UNIX SOCKET and pipes the raw stream over ssh's
stdin/stdout. A socketpair bridges those pipes to http.client, so the rest
of the app talks plain HTTP to an ordinary socket — no port is ever opened
locally, and the servers themselves never open a TCP port on ANY interface.
Locally (no ssh host) it is a plain AF_UNIX connect.

run() executes short shell scripts over the same multiplexed connection (or
locally when no host is set) — the llama-server lifecycle manager (srv.py)
is built on it. Hold keeps a script's stdin open as a LIFELINE: the far
side sees EOF the instant this process dies, however it dies.

Cost control: channels multiplex over ONE master connection per SSH host
(ControlMaster=auto; control sockets live in ~/.loom/run; ControlPersist
keeps the master alive briefly between requests). The first request
authenticates; every later channel is milliseconds.

Auth is the SYSTEM ssh's job — ~/.ssh/config aliases, agent keys, ProxyJump
all apply, and passphrase / host-key prompts surface in-app through the
askpass broker (askpass.py).

Error mapping: failures raise urllib.error.HTTPError / URLError so callers
handle tunneled and direct requests with the SAME except clauses.
"""

from __future__ import annotations

import http.client
import os
import queue
import socket
import ssl
import subprocess
import threading
import urllib.error
import urllib.parse

from loom import askpass, store

CONNECT_TIMEOUT_S = 15          # ssh connection establishment cap
CONTROL_PERSIST_S = 60          # idle master lifetime
_PUMP_CHUNK = 65536

# override point for tests: a stub that speaks the protocol on stdio can
# stand in for the real ssh binary (no sshd needed to vet the plumbing)
SSH_CMD = ["ssh"]


def _ssh_env() -> dict:
    """ssh with in-app prompts: passphrases and host-key confirmations go
    through the askpass broker."""
    env = dict(os.environ)
    helper = askpass.helper_path()
    env["SSH_ASKPASS"] = str(helper)
    env["SSH_ASKPASS_REQUIRE"] = "force"   # OpenSSH >= 8.4
    env["LOOM_ASKPASS_SOCK"] = str(askpass.socket_path())
    env.setdefault("DISPLAY", ":0")        # some builds gate SSH_ASKPASS on it
    return env


def _mux_args() -> list[str]:
    run = store.run_dir()
    return [
        "-o", "BatchMode=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={run}/sshmux-%C",
        "-o", f"ControlPersist={CONTROL_PERSIST_S}",
        "-o", "ServerAliveInterval=30",
    ]


def _check_host(ssh_host: str) -> None:
    if ssh_host.startswith("-"):
        # would be parsed as an ssh FLAG, not a destination — flags
        # belong in ~/.ssh/config, and this closes an argv-injection hole
        raise urllib.error.URLError(
            f"invalid SSH host {ssh_host!r} — use user@host, a bare host, or a "
            "~/.ssh/config alias (flags are not accepted here)")


def unix_bridge_cmd(sock_path: str) -> str:
    """The remote stdio↔unix-socket bridge: whichever of socat / OpenBSD nc /
    python3 exists on the far machine. The socket path is app-generated
    ([-_./a-z0-9]) and single-quoted anyway."""
    p = sock_path.replace("'", "'\\''")
    py = ("import socket,sys,threading,shutil\n"
          "s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])\n"
          "def up():\n"
          " try:\n"
          "  shutil.copyfileobj(sys.stdin.buffer, s.makefile('wb'), 65536)\n"
          " except OSError: pass\n"
          " try: s.shutdown(socket.SHUT_WR)\n"
          " except OSError: pass\n"
          "t=threading.Thread(target=up,daemon=True); t.start()\n"
          "try:\n"
          " shutil.copyfileobj(s.makefile('rb'), sys.stdout.buffer, 65536)\n"
          "except OSError: pass\n")
    return ("if command -v socat >/dev/null 2>&1; then exec socat - UNIX-CONNECT:'" + p + "'; "
            "elif command -v nc >/dev/null 2>&1 && nc -h 2>&1 | grep -q -- -U; then exec nc -U '" + p + "'; "
            "else exec python3 -c " + _sh_quote(py) + " '" + p + "'; fi")


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


class _Channel:
    """One ssh stdio subprocess bridged to a local socketpair.

    The far end is a small bridge command (unix_bridge_cmd) to the server's
    unix socket. `sock` is the caller's end — a real socket, so http.client
    gets timeouts and file-like semantics for free. Two pump threads shuttle
    bytes between the other end and the ssh process; closing the channel
    tears everything down (and unblocks any read stuck on the socket)."""

    def __init__(self, ssh_host: str, unix: str):
        self._stderr = b""
        _check_host(ssh_host)
        argv = [*SSH_CMD, *_mux_args(), ssh_host, unix_bridge_cmd(unix)]
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=_ssh_env(), start_new_session=True)
        except FileNotFoundError:
            raise urllib.error.URLError(
                "ssh executable not found — the SSH tunnel needs an OpenSSH client on PATH")
        self.sock, self._far = socket.socketpair()
        threading.Thread(target=self._pump_out, daemon=True, name="sshtun-out").start()
        threading.Thread(target=self._pump_in, daemon=True, name="sshtun-in").start()
        threading.Thread(target=self._pump_err, daemon=True, name="sshtun-err").start()

    def _pump_out(self):   # local writes → ssh stdin
        try:
            while True:
                data = self._far.recv(_PUMP_CHUNK)
                if not data:
                    break
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _pump_in(self):    # ssh stdout → local reads
        try:
            while True:
                data = self.proc.stdout.read1(_PUMP_CHUNK)
                if not data:
                    break
                self._far.sendall(data)
        except (OSError, ValueError):
            pass
        # EOF from ssh (clean close OR the connection failed): half-close so
        # a blocked local read returns instead of hanging
        try:
            self._far.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _pump_err(self):
        try:
            self._stderr = self.proc.stderr.read() or b""
        except (OSError, ValueError):
            pass

    def error_hint(self) -> str:
        """ssh's own words for why the channel died (auth refused, no route,
        connection refused on the remote side, …)."""
        lines = [l for l in self._stderr.decode("utf-8", "replace").splitlines()
                 if l.strip() and not l.startswith("Warning: Permanently added")]
        return lines[-1].strip() if lines else ""

    def close(self):
        for s in (self.sock, self._far):
            try:
                s.close()
            except OSError:
                pass
        try:
            self.proc.terminate()
        except OSError:
            return
        # bounded reap so a terminate-ignoring ssh can't linger as a live
        # process (and zombie) until GC happens to collect the channel
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except OSError:
            pass


class _TunnelHTTPConnection(http.client.HTTPConnection):
    """http.client connection whose transport is a remote unix socket over
    an ssh stdio channel, or a LOCAL unix socket (no ssh process at all —
    a plain AF_UNIX connect; nothing listens on TCP)."""

    def __init__(self, host: str, port: int, ssh_host: str, timeout: float,
                 unix: str):
        super().__init__(host, port, timeout=timeout)
        self._ssh_host = ssh_host
        self._unix = unix
        self._chan: _Channel | None = None

    def connect(self):
        if not self._ssh_host:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(self._unix)
            except OSError as e:
                sock.close()
                raise urllib.error.URLError(
                    f"unix socket {self._unix}: {e.strerror or e}")
            self.sock = sock
            return
        self._chan = _Channel(self._ssh_host, self._unix)
        sock = self._chan.sock
        sock.settimeout(self.timeout)
        self.sock = sock

    # NOTE deliberately NOT overriding close(): http.client calls
    # conn.close() ITSELF while constructing a Connection:close response
    # ("the connection passes to the response") — tearing the channel down
    # there kills the ssh process mid-stream and the body never arrives.
    # The response's close — via request()'s close_all — owns teardown.
    def shutdown_channel(self):
        if self._chan is not None:
            self._chan.close()
            self._chan = None


def request(method: str, url: str, headers: dict | None, body: bytes | None,
            timeout: float, ssh_host: str, unix: str,
            abort_box: dict | None = None):
    """One HTTP request to a llama-server on a unix socket (remote over an
    ssh channel when ssh_host is set, local AF_UNIX otherwise). Returns
    http.client.HTTPResponse (the same class urllib returns, so
    iteration/read/close behave identically). 4xx/5xx raise
    urllib.error.HTTPError; transport problems raise urllib.error.URLError
    carrying ssh's stderr hint.

    abort_box: caller-owned dict — this call stores an ``abort`` callable
    in it BEFORE sending, so another thread can rip the connection down
    while we are still blocked waiting for response headers (a busy
    server processing a huge prompt sends nothing for a long time; a
    cancel must not have to wait that out)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http":
        raise urllib.error.URLError(f"unsupported scheme for the socket tunnel: {u.scheme}")
    host = u.hostname or "127.0.0.1"
    port = u.port or 80
    path = (u.path or "/") + (("?" + u.query) if u.query else "")

    conn = _TunnelHTTPConnection(host, port, ssh_host, timeout, unix)
    hdrs = dict(headers or {})
    hdrs.setdefault("Connection", "close")   # one channel per request — no keep-alive
    if abort_box is not None:
        def _abort():
            # close() alone does NOT wake a thread blocked in recv() —
            # shutdown() does, and the reader then sees a clean EOF
            try:
                s = conn.sock
                if s is not None:
                    s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            finally:
                conn.shutdown_channel()
        abort_box["abort"] = _abort
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
    except (http.client.HTTPException, OSError) as e:
        hint = conn._chan.error_hint() if conn._chan else ""
        conn.shutdown_channel()
        raise urllib.error.URLError(
            (f"ssh tunnel via {ssh_host}: " if ssh_host else "")
            + str(hint or e))
    # the response OWNS the channel from here: closing it tears the ssh
    # process down — a client that abandons a stream mid-read relies on
    # close() unblocking it
    orig_close = resp.close

    def close_all():
        try:
            orig_close()
        finally:
            conn.shutdown_channel()
    resp.close = close_all
    if resp.status >= 400:
        # a REAL urllib HTTPError: callers' existing except-clauses (and
        # e.read() for the error body) work unchanged
        raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                     resp.headers, resp)
    return resp


class RunError(Exception):
    """A script could not be EXECUTED (ssh/auth/transport) — distinct from a
    script that ran and returned a nonzero exit code."""


class HoldError(Exception):
    """A held script could not be started, or never answered."""


class Hold:
    """A script whose stdin is kept OPEN as a LIFELINE.

    The far side runs until its stdin reaches EOF — and the kernel closes
    this process's pipe ends the instant it dies, however it dies (clean
    exit, crash, SIGKILL). Over ssh the EOF rides the channel to the remote
    command's stdin. No cooperation from the dying process is needed, which
    is the property signals/atexit/heartbeats can't give.

    srv.py builds each llama-server's supervisor on this: the held script
    spawns the server as its child, blocks reading stdin, and kills the
    server when the lifeline drops. stdout comes back line-by-line through
    line(); on_exit (if set) fires from the reader thread when the script
    ends, so the app learns its server is gone even when something ELSE
    killed it.

    NOTE for scripts: bash reads an incrementally-fed stdin script byte by
    byte, so anything in the script that reads stdin races bash for the
    remaining script text. Wrap the whole script in `{ ... }` — bash then
    parses (consumes) the entire block before executing a line of it, and
    only the lifeline is left on the pipe."""

    def __init__(self, ssh_host: str, script: str, on_exit=None):
        self.on_exit = on_exit
        self._stderr = b""
        self._lines: queue.Queue = queue.Queue()
        if ssh_host:
            if ssh_host.startswith("-"):
                raise HoldError(f"invalid SSH host {ssh_host!r}")
            argv = [*SSH_CMD, *_mux_args(), ssh_host, "bash -s"]
        else:
            argv = ["bash", "-s"]
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=_ssh_env() if ssh_host else None,
                start_new_session=True)
        except FileNotFoundError as e:
            raise HoldError(f"{argv[0]} not found: {e}")
        try:
            self.proc.stdin.write(script.encode("utf-8"))
            self.proc.stdin.flush()   # and stdin stays open — that IS the point
        except OSError as e:
            self.close()
            raise HoldError(f"could not deliver the script: {e}")
        threading.Thread(target=self._read_out, daemon=True, name="hold-out").start()
        threading.Thread(target=self._read_err, daemon=True, name="hold-err").start()

    def _read_out(self):
        try:
            for raw in self.proc.stdout:
                self._lines.put(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait()
        except OSError:
            pass
        self._lines.put(None)          # sentinel: the script is gone
        cb = self.on_exit
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _read_err(self):
        try:
            self._stderr = self.proc.stderr.read() or b""
        except (OSError, ValueError):
            pass

    def error_hint(self) -> str:
        lines = [l for l in self._stderr.decode("utf-8", "replace").splitlines()
                 if l.strip() and not l.startswith("Warning: Permanently added")]
        return lines[-1].strip() if lines else ""

    def line(self, timeout: float) -> str | None:
        """Next stdout line; None once the script has exited."""
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            hint = self.error_hint()
            raise HoldError(f"no answer after {int(timeout)}s"
                            + (f" — {hint}" if hint else ""))

    def alive(self) -> bool:
        return self.proc.poll() is None

    def release(self) -> None:
        """Cut the lifeline deliberately: the far side sees EOF and winds
        down (a supervisor kills its server). The process is left to exit
        on its own; the reader thread reaps it."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def close(self) -> None:
        """release() plus a hard stop of the local process — error paths."""
        self.release()
        try:
            self.proc.terminate()
        except OSError:
            pass


def run(ssh_host: str, script: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run a bash script on `ssh_host` (over the multiplexed master — first
    call authenticates, later ones are milliseconds) or LOCALLY when
    ssh_host is empty ("this machine" is just another host, minus the ssh).
    The script travels on stdin (`bash -s`) so no quoting layer can mangle
    it. Returns (exit_code, stdout, stderr); raises RunError only when
    execution itself failed (no ssh binary, timeout)."""
    if ssh_host:
        if ssh_host.startswith("-"):
            raise RunError(f"invalid SSH host {ssh_host!r}")
        argv = [*SSH_CMD, *_mux_args(), ssh_host, "bash -s"]
    else:
        argv = ["bash", "-s"]
    try:
        proc = subprocess.run(
            argv, input=script.encode("utf-8"), capture_output=True,
            timeout=timeout, env=_ssh_env() if ssh_host else None,
            start_new_session=True)
    except FileNotFoundError as e:
        raise RunError(f"{argv[0]} not found: {e}")
    except subprocess.TimeoutExpired:
        raise RunError(f"timed out after {int(timeout)}s"
                       + (f" (via ssh {ssh_host})" if ssh_host else ""))
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))
