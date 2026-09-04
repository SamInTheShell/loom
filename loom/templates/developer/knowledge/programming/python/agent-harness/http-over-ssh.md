# HTTP over SSH - `ssh -W` stdio channels, no local listener

How to reach an HTTP service on another machine - a remote LLM
server (llm-providers.md), an internal API - through SSH without
opening any local port. The classic `ssh -L localhost:1234:…`
forward needs a real localhost port that lingers, collides with
other apps, and is usable by EVERY local process. Instead, each
HTTP request rides its own `ssh -W host:port` stdio channel
(OpenSSH "netcat mode"): ssh connects to the destination FROM the
remote machine and pipes the raw TCP stream over its own
stdin/stdout. A socketpair bridges those pipes to `http.client`,
so the rest of the app talks plain HTTP(S) to an ordinary socket.
Bonus semantics: the target hostname resolves on the REMOTE side -
`127.0.0.1:11434` means "that machine's localhost", exactly what a
self-hosted model server wants.

## The channel: socketpair bridging

One channel = one ssh subprocess + one `socket.socketpair()`. The
caller gets one end - a REAL socket, so `http.client` and `ssl` get
timeouts and file-like semantics for free. Pump threads shuttle
bytes between the other end and the process:

```python
proc = subprocess.Popen([*SSH_CMD, *mux_args,
                         "-W", f"{host}:{port}", ssh_host],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE,
                        env=ssh_env(), start_new_session=True)
sock, far = socket.socketpair()
# thread 1: far.recv() -> proc.stdin.write() + flush   (requests out)
# thread 2: proc.stdout.read1() -> far.sendall()       (response in)
# thread 3: collect proc.stderr for the error hint
```

Details that matter:

- On EOF from ssh stdout (clean close OR failed connection), the
  in-pump must HALF-CLOSE its end (`far.shutdown(socket.SHUT_WR)`)
  so a local read blocked on the socket returns instead of hanging.
- Keep stderr: when the channel dies, ssh's own words ("Permission
  denied", "Connection refused", "No route to host") are the only
  diagnosis. Filter the noise ("Warning: Permanently added …") and
  report the last meaningful line.
- Reject an ssh_host starting with `-`: it would parse as an ssh
  FLAG, not a destination - an argv-injection hole. Flags belong
  in `~/.ssh/config`.
- Make the ssh command an override point (a module-level list): a
  stub script speaking the protocol on stdio stands in for real
  ssh in tests - the whole bridge is vettable with no sshd.

## http.client on top

Subclass `http.client.HTTPConnection` and override only
`connect()`: create the channel, `settimeout()` on its socket, and
for https wrap it with `ssl.create_default_context()` passing
`server_hostname` - TLS still verifies the TARGET's certificate
end-to-end; ssh only carries bytes. Default the `Connection: close`
header: one channel per request, no keep-alive to manage.

Hard-won teardown rule: do NOT override `Connection.close()` to
kill the channel. `http.client` calls `conn.close()` ITSELF while
constructing a `Connection: close` response ("the connection passes
to the response") - tearing the ssh process down there kills the
stream mid-body: first chunk delivered, then silence, no error.
Ownership goes to the RESPONSE instead: wrap `resp.close` so it
first runs the original close, then shuts the channel down. That
same wrapper is what makes a Stop button work - closing the
response tears down the ssh process and instantly unblocks a read
stuck mid-stream (llm-providers.md's SSE loop relies on it).

## Cost control: ControlMaster multiplexing

A fresh ssh handshake per HTTP request would be seconds each.
Channels multiplex over ONE master connection per ssh host:

```
-o ControlMaster=auto
-o ControlPath=<private run dir>/sshmux-%C
-o ControlPersist=60
-o ConnectTimeout=15
-o ServerAliveInterval=30
-o StrictHostKeyChecking=accept-new
```

The first request authenticates and becomes the master; every later
`-W` is a channel over it - milliseconds. `ControlPersist` keeps
the idle master alive briefly between requests, then it exits by
itself; nothing to manage or leak. Control sockets go in a private
per-app runtime dir (`%C` hashes host+port+user into the name).
`accept-new` pins hosts on first contact without prompting, while
still refusing CHANGED keys.

## Auth: the system ssh owns it

Never implement SSH auth. Spawning the system `ssh` binary means
`~/.ssh/config` aliases, agent keys, `ProxyJump`, hardware tokens -
everything the user already configured - just works, and the app
never touches key material. The one integration point is prompts:
set `SSH_ASKPASS` to a helper executable, `SSH_ASKPASS_REQUIRE=
force` (OpenSSH ≥ 8.4) so it is used even with a TTY, and default
`DISPLAY` if unset (some builds gate askpass on it). The helper
relays the prompt over a local socket to the app's askpass broker -
the same one git network operations use (git-depot.md) - so
passphrase and host-key questions appear as in-app dialogs instead
of hanging an invisible subprocess.

## Error mapping

Make tunnel failures raise the SAME exceptions direct requests do,
so callers keep one set of except clauses for both transports:

- HTTP status ≥ 400 → raise a real `urllib.error.HTTPError(url,
  status, reason, headers, resp)` - callers' `e.read()` for the
  error body works unchanged.
- Transport failure (socket error, ssh died) → `urllib.error.
  URLError` carrying the ssh stderr hint: `"ssh tunnel via {host}:
  {hint}"`.
- ssh binary missing (`FileNotFoundError` from Popen) → URLError
  saying an OpenSSH client is required on PATH.
- Non-http(s) schemes rejected up front; default ports 80/443.

The entry point is one function - `request(method, url, headers,
body, timeout, ssh_host)` returning an `http.client.HTTPResponse`
(the class urllib itself returns, so iteration/read/close behave
identically). Callers branch once on "tunnel configured?" and
nothing else changes.

## Rules

- One request, one `-W` channel; the master connection is the
  reuse layer. Never open a local listening port.
- The response owns channel teardown - never the connection's
  `close()`; http.client closes connections mid-parse.
- Half-close the socketpair on ssh EOF or blocked reads hang.
- TLS wraps INSIDE the tunnel with real certificate verification;
  ssh is transport, not trust.
- System ssh does auth; the app only brokers prompts via askpass
  (git-depot.md). Reject `-`-prefixed hosts.
- Map everything to urllib exceptions and carry ssh's stderr in
  them; "tunnel failed" without ssh's words is undebuggable.
- Keep the ssh argv injectable for tests; a stdio stub replaces
  sshd entirely.
