"""HTTP over SSH stdio forwarding - reach inference APIs on other machines
with NO local listener.

The classic `ssh -L localhost:1234:...` needs a real localhost port that
lingers, collides with other apps, and is usable by every local process.
Instead, each HTTP request here rides its own ssh stdio channel: a tiny
bridge on the far side (socat → nc → python3, whichever exists) connects
to the target TCP address AS SEEN FROM THAT HOST and pipes the raw stream
over ssh's stdin/stdout. A socketpair bridges those pipes to http.client,
so the rest of the app talks plain HTTP to an ordinary socket - no port is
ever opened locally. Without an ssh host it is a plain TCP connect.

AUTH IS KEYS ONLY: ssh runs with BatchMode=yes, so a destination that
would prompt for a password or an unloaded-key passphrase FAILS
immediately ("Permission denied (publickey...)") instead of hanging on a
prompt nobody can answer. Load keys into an agent (ssh-agent / ssh-add)
or use unencrypted key files; ~/.ssh/config aliases and ProxyJump apply
as usual.

Cost control: channels multiplex over ONE master connection per SSH host
(ControlMaster=auto; control sockets live in ~/.loom/run; ControlPersist
keeps the master alive briefly between requests). The first request
authenticates; every later channel is milliseconds.

Error mapping: failures raise urllib.error.HTTPError / URLError so callers
handle tunneled and direct requests with the SAME except clauses.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import threading
import urllib.error
import urllib.parse

from loom import store

CONNECT_TIMEOUT_S = 15          # ssh connection establishment cap
CONTROL_PERSIST_S = 60          # idle master lifetime
_PUMP_CHUNK = 65536

# override point for tests: a stub that speaks the protocol on stdio can
# stand in for the real ssh binary (no sshd needed to vet the plumbing)
SSH_CMD = ["ssh"]


def _mux_args() -> list[str]:
    run = store.run_dir()
    return [
        # keys only - a host that needs a password must FAIL, not prompt
        "-o", "BatchMode=yes",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={run}/sshmux-%C",
        "-o", f"ControlPersist={CONTROL_PERSIST_S}",
        "-o", "ServerAliveInterval=30",
    ]


def _check_host(ssh_host: str) -> None:
    if ssh_host.startswith("-"):
        # would be parsed as an ssh FLAG, not a destination - flags
        # belong in ~/.ssh/config, and this closes an argv-injection hole
        raise urllib.error.URLError(
            f"invalid SSH host {ssh_host!r} - use user@host, a bare host, or a "
            "~/.ssh/config alias (flags are not accepted here)")


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def tcp_bridge_cmd(host: str, port: int) -> str:
    """The remote stdio↔TCP bridge: whichever of socat / nc / python3
    exists on the far machine, connected to host:port AS SEEN FROM THERE."""
    h = str(host).replace("'", "")
    p = int(port)
    py = ("import socket,sys,threading,shutil\n"
          "s=socket.create_connection((sys.argv[1],int(sys.argv[2])))\n"
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
    return (f"if command -v socat >/dev/null 2>&1; then exec socat - TCP:'{h}':{p}; "
            f"elif command -v nc >/dev/null 2>&1; then exec nc '{h}' {p}; "
            "else exec python3 -c " + _sh_quote(py) + f" '{h}' {p}; fi")


class _Channel:
    """One ssh stdio subprocess bridged to a local socketpair.

    The far end is a small bridge command (tcp_bridge_cmd) to the target's
    TCP address. `sock` is the caller's end - a real socket, so http.client
    gets timeouts and file-like semantics for free. Two pump threads shuttle
    bytes between the other end and the ssh process; closing the channel
    tears everything down (and unblocks any read stuck on the socket)."""

    def __init__(self, ssh_host: str, host: str, port: int):
        self._stderr = b""
        _check_host(ssh_host)
        argv = [*SSH_CMD, *_mux_args(), ssh_host, tcp_bridge_cmd(host, port)]
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ), start_new_session=True)
        except FileNotFoundError:
            raise urllib.error.URLError(
                "ssh executable not found - the SSH tunnel needs an OpenSSH client on PATH")
        self.sock, self._far = socket.socketpair()
        threading.Thread(target=self._pump_out, daemon=True, name="sshtun-out").start()
        threading.Thread(target=self._pump_in, daemon=True, name="sshtun-in").start()
        self._err_thread = threading.Thread(target=self._pump_err,
                                            daemon=True, name="sshtun-err")
        self._err_thread.start()

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
        connection refused on the remote side, …) - with the BatchMode
        password-denied case translated into an actionable sentence.
        Waits briefly for the stderr pump: the error usually surfaces a
        beat before ssh's last words land."""
        self._err_thread.join(timeout=1.5)
        lines = [l for l in self._stderr.decode("utf-8", "replace").splitlines()
                 if l.strip() and not l.startswith("Warning: Permanently added")]
        hint = lines[-1].strip() if lines else ""
        if "Permission denied" in hint:
            hint += (" - Loom connects with SSH KEYS ONLY (no password "
                     "prompts). Load a key into ssh-agent for this host.")
        return hint

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
    """http.client connection whose transport is a TCP target over an ssh
    stdio channel, or a plain LOCAL TCP connect (no ssh process at all)."""

    def __init__(self, host: str, port: int, ssh_host: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ssh_host = ssh_host
        self._chan: _Channel | None = None

    def connect(self):
        if not self._ssh_host:
            try:
                sock = socket.create_connection((self.host, self.port),
                                                timeout=self.timeout)
            except OSError as e:
                raise urllib.error.URLError(
                    f"{self.host}:{self.port}: {e.strerror or e}")
            self.sock = sock
            return
        self._chan = _Channel(self._ssh_host, self.host, self.port)
        sock = self._chan.sock
        sock.settimeout(self.timeout)
        self.sock = sock

    # NOTE deliberately NOT overriding close(): http.client calls
    # conn.close() ITSELF while constructing a Connection:close response
    # ("the connection passes to the response") - tearing the channel down
    # there kills the ssh process mid-stream and the body never arrives.
    # The response's close - via request()'s close_all - owns teardown.
    def shutdown_channel(self):
        if self._chan is not None:
            self._chan.close()
            self._chan = None


def request(method: str, url: str, headers: dict | None, body: bytes | None,
            timeout: float, ssh_host: str = "",
            abort_box: dict | None = None):
    """One HTTP request to an inference API (remote over an ssh channel
    when ssh_host is set, plain TCP otherwise; the URL's host:port is
    resolved ON the ssh host when tunneled). Returns
    http.client.HTTPResponse (the same class urllib returns, so
    iteration/read/close behave identically). 4xx/5xx raise
    urllib.error.HTTPError; transport problems raise urllib.error.URLError
    carrying ssh's stderr hint.

    abort_box: caller-owned dict - this call stores an ``abort`` callable
    in it BEFORE sending, so another thread can rip the connection down
    while we are still blocked waiting for response headers (a busy
    server processing a huge prompt sends nothing for a long time; a
    cancel must not have to wait that out)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise urllib.error.URLError(f"unsupported scheme: {u.scheme}")
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")

    if u.scheme == "https":
        if ssh_host:
            raise urllib.error.URLError(
                "https over the ssh tunnel is not supported - tunneled "
                "providers must use plain http (the ssh channel is the "
                "encryption layer)")
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = _TunnelHTTPConnection(host, port, ssh_host, timeout)
    hdrs = dict(headers or {})
    hdrs.setdefault("Connection", "close")   # one channel per request - no keep-alive
    if abort_box is not None:
        def _abort():
            # close() alone does NOT wake a thread blocked in recv() -
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
                if hasattr(conn, "shutdown_channel"):
                    conn.shutdown_channel()
        abort_box["abort"] = _abort
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
    except (http.client.HTTPException, OSError) as e:
        hint = ""
        if getattr(conn, "_chan", None) is not None:
            hint = conn._chan.error_hint()
        if hasattr(conn, "shutdown_channel"):
            conn.shutdown_channel()
        raise urllib.error.URLError(
            (f"ssh tunnel via {ssh_host}: " if ssh_host else "")
            + str(hint or e))
    # the response OWNS the channel from here: closing it tears the ssh
    # process down - a client that abandons a stream mid-read relies on
    # close() unblocking it
    orig_close = resp.close

    def close_all():
        try:
            orig_close()
        finally:
            if hasattr(conn, "shutdown_channel"):
                conn.shutdown_channel()
    resp.close = close_all
    if resp.status >= 400:
        # a REAL urllib HTTPError: callers' existing except-clauses (and
        # e.read() for the error body) work unchanged
        raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                     resp.headers, resp)
    return resp
