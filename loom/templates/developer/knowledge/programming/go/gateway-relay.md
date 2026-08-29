# Gateway relay — public ingress for a node behind NAT

A gateway relay gives a self-hosted node public HTTP(S) ingress with
ZERO inbound ports on the node's network: a tiny always-public server
(the gateway) terminates public TLS on 80/443, and the private node
dials OUT to it, holding a pool of persistent reverse tunnels. Each
public request becomes one multiplexed stream down a tunnel; the node
serves it and the response streams back. The gateway is deliberately
dumb — it holds no application state, learns the node's address never
(the node always dials), and is configured entirely by push from the
node. Pairing (key exchange, signed requests, sealed pushes) is
pairing-crypto.md; this file is the relay itself. postoffice.md is the
same pattern applied to SMTP.

Library: `github.com/hashicorp/yamux` (stream multiplexing over one
TCP/TLS connection; both sides can open streams, keepalives built in).

```
go get github.com/hashicorp/yamux
```

## Architecture — four listeners, one dial direction

The gateway binary runs four listeners on the public VM:

- `:80` / `:443` — the public edge browsers hit.
- tunnel port (e.g. `:7443`) — the node's stream pool dials it; TLS
  with the gateway's self-signed cert, fingerprint-pinned by the node.
- control port (e.g. `:7444`) — an HTTPS API the node dials to push
  config and certificates and to poll status. Same pinned cert.

Authority flows node → gateway, always over connections the NODE
opened. The gateway's local config is just listen addresses and a data
dir; hostnames, TLS modes, limits, the offline page, and certificates
all arrive by push. One gateway pairs with exactly one node identity;
re-pairing requires wiping the gateway's data dir (a compromised
console cannot silently re-key a running gateway).

## Trust boundary — TLS terminates at the gateway, keys stay in RAM

Public HTTPS terminates ON THE GATEWAY: it holds each hostname's
serving certificate and answers SNI from an in-RAM store. So the
gateway sees plaintext HTTP in memory — the honest statement of the
model is "at rest blind, in RAM transient plaintext":

- Certificate private keys arrive as sealed pushes and live in RAM
  ONLY, never written to the gateway disk. A stolen gateway disk
  yields no keys; a gateway restart cannot serve HTTPS for a hostname
  until the node re-pushes (its sync loop notices and does — below).
- The config push (hostnames, TLS modes, limits, offline page) is
  keyless BY CONSTRUCTION — certificate material rides a separate
  message type — so the gateway caches it to disk verbatim and a
  restart restores routing and the offline page immediately.
- Raw TCP relays (below) are never parsed: an end-to-end encrypted
  protocol (SSH) stays opaque in flight and at rest on the gateway.

ACME runs on the NODE, not the gateway. The node runs the ACME client
(`golang.org/x/crypto/acme`, HTTP-01); the CA's challenge probe hits
the gateway on port 80 at `/.well-known/acme-challenge/…` and tunnels
through like any request to an unauthenticated challenge handler on
the node. Issuance and renewal therefore need nothing installed on the
gateway. Publish the challenge token to shared storage BEFORE
accepting the challenge — with multiple node replicas the probe may
arrive at any of them. Issued certs land in node storage; the sync
loop notices the new NotAfter and pushes. Three TLS modes per
hostname: `acme`, `selfsigned` (node mints and pushes), `custom`
(operator-uploaded, never auto-renewed).

## Control plane — three endpoints, all signed

Every control request carries a signature over method + full request
URI + body under the node's control key (pairing-crypto.md); the
gateway's verifier holds exactly one public key, enforcing the
one-node invariant. Push bodies are additionally sealed to the
gateway's X25519 key. The whole API is three routes:

- `GET /v1/status` — self-report: version, uptime, applied config
  serial, per-hostname cert-in-RAM + expiry, live tunnel count, open
  streams, request/4xx/5xx/offline counters, and a RAM ring of the
  last 50 operational errors. This is the gateway's ONLY telemetry;
  the node polls it and persists samples.
- `PUT /v1/config` — sealed declarative desired state with a
  monotonic `serial`; gateway applies (RAM swap + disk cache + limiter
  retune), acks the serial.
- `PUT /v1/certs` — one sealed `{hostname, cert_pem, key_pem}`;
  installed RAM-only, ack carries the leaf's NotAfter.

Serve the control (and tunnel) listener with a self-signed cert and
let the client pin its SHA-256 fingerprint via
`InsecureSkipVerify: true` + `VerifyPeerCertificate` comparing the
leaf DER hash — chain and name checks are meaningless for a cert
exchanged by hand at pairing. Filter `"TLS handshake error"` lines out
of `http.Server.ErrorLog`: an exposed port collects constant scanner
probes and the rejection is correct, not news.

## Tunnel lifecycle — dial, hello, yamux

The node keeps N (default 4) independent connections per gateway. Each
slot loops dial → serve → jittered exponential backoff (1s doubling to
30s cap; sleep a random duration in `[b/2, b)` so slots never
thundering-herd a restarted gateway).

One connection's handshake, node side:

```go
conn, _ := tls.DialWithDialer(&net.Dialer{Timeout: 10 * time.Second},
    "tcp", tunnelAddr, pinnedTLSCfg)
auth, _ := SignRequest(ctlPriv, "TUNNEL", "/v1/tunnel", nil)
raw, _ := json.Marshal(HelloFrame{V: 1, Auth: auth})
conn.Write(append(raw, '\n'))
// read the one-line {ok} reply BYTE-BY-BYTE: yamux frames follow
// immediately after the newline; a buffered reader would eat them.
```

The hello is "a signed request in disguise": a fixed pseudo
method/path signed with the same key and verified by the same
verifier (same nonce replay cache) as the control plane, so only the
paired node can join the pool. The gateway reads one JSON line under a
10 s deadline (a dialer that connects and says nothing doesn't get to
hold a socket), verifies, answers one JSON line, clears the deadline.
Gateway-side it read the hello through a `bufio.Reader` — wrap the
conn so the reader's buffered bytes replay in front before yamux
takes over.

Then yamux. TCP roles pick the yamux side (stream opening is
symmetric, so the choice is free): the gateway accepted, so

```go
sess, _ := yamux.Server(conn, cfg)   // gateway
sess, _ := yamux.Client(conn, cfg)   // node
```

with `cfg := yamux.DefaultConfig()` and both `LogOutput`/`Logger`
pointed at `io.Discard` (protocol noise stays out of your logs).
Leave keepalives at the default (enabled, 30 s interval): a dead NAT
mapping closes the session, `IsClosed()` flips, the pool prunes it
and the slot redials. The gateway pools sessions and round-robins
`sess.Open()` across healthy ones, pruning closed sessions on every
pick; nil from the pick means "node offline". With multiple node
replicas each runs its own pool by design — the gateway just sees
replicas×N sessions and any replica can serve any request.

The node serves its half with a plain `http.Server` — a
`*yamux.Session` is a `net.Listener` whose `Accept` yields streams:

```go
srv := &http.Server{Handler: markTunnel(router),
    ReadHeaderTimeout: 10 * time.Second}
err := srv.Serve(sess)               // or a demux wrapper, below
if err == yamux.ErrSessionShutdown || sess.IsClosed() { /* orderly */ }
```

## Per-stream HTTP proxying

One stream = one HTTP exchange. The gateway forwards a public request
by cloning it and driving a throwaway transport whose dialer returns
the already-open stream:

```go
out := r.Clone(r.Context())
out.URL.Scheme, out.URL.Host, out.RequestURI = "http", r.Host, ""
out.Header.Set("X-Forwarded-For", clientIP(r))   // SET, never append
out.Header.Set("X-Forwarded-Proto", scheme)
out.Header.Set("X-Forwarded-Host", hostOnly(r.Host))
tr := &http.Transport{
    DialContext: func(...) (net.Conn, error) { return stream, nil },
    DisableKeepAlives: true, MaxIdleConns: 1,
    ResponseHeaderTimeout: 60 * time.Second,
}
resp, err := tr.RoundTrip(out)
```

`DisableKeepAlives` makes the transport send `Connection: close`, so
the node's server ends the stream after one response — the stream IS
the connection and dies with the exchange. The gateway is the edge:
it OVERWRITES the X-Forwarded-* headers so the node sees exactly one
trustworthy hop; node-side, wrap the tunnel-served handler in a
context marker (`context.WithValue` set by the wrapping handler) and
trust those headers ONLY when the marker is present. The trust rides
the server the request arrived on — no header can forge it, and a
directly-exposed listener stays untrusting.

Response path: copy headers minus hop-by-hop fields (Connection,
Keep-Alive, Transfer-Encoding, Upgrade, Proxy-*, Te, Trailer), write
the status, then copy the body with a `Flush()` after EVERY chunk —
without it SSE events pool in the edge buffer until the handler
returns, which defeats them. A `101 Switching Protocols` response
(WebSocket) switches to splice mode: `http.Hijacker` the client conn,
clear its deadlines, replay the response head verbatim, then
`io.Copy` both directions until either side hangs up (the response
Body of a 101 is an `io.ReadWriteCloser`). Because upgrades must pass
through verbatim and the tunnel speaks HTTP/1, the public :443
listener advertises `NextProtos: ["http/1.1"]` only — no h2.

## Multi-tenant routing and failure modes

The pushed hostname list is the routing table. Per request:

- `:443`: SNI → RAM cert store. Unknown/certless SNI serves a
  fallback self-signed cert minted fresh at boot so the handshake
  COMPLETES and the HTTP layer can answer `421 Misdirected Request` —
  a clean answer beats a protocol-level mystery. Known Host → tunnel.
- `:80`: ACME challenge paths tunnel UNCONDITIONALLY (even for
  hostnames not yet pushed, so mid-setup issuance works); hostnames
  with force-HTTPS get a `301`; the rest tunnel as-is; unknown → 421.
- Hostname lookup is case-insensitive with the port stripped (careful
  with bare IPv6 literals when splitting).

No live session → serve the offline page: `503` with `Retry-After`
and `Cache-Control: no-store`, HTML pushed by the node and cached to
gateway disk (with a built-in default before any push). There is no
queueing — fail fast and let the client retry. Raw TCP relays have no
offline page possible: accept-and-close is the whole answer.

## Abuse controls at the edge

All limits arrive in the config push and retune LIVE (atomics read on
every decision):

- Per-IP token bucket, requests/minute with rate == burst, answered
  with `429` + `Retry-After`. Bound the bucket map (e.g. 100 k keys)
  and, at the cap, sweep buckets that have refilled to full — they
  carry no throttling state, so deleting them is behavior-neutral. An
  attacker rotating source IPs must not grow memory unboundedly.
- A global concurrent-connection gate enforced at ACCEPT time by a
  wrapping `net.Listener`: over the cap, close the conn on the spot
  and keep accepting (fail fast beats a hung tab). Release exactly
  once via `sync.Once` in the conn's `Close`.
- `http.MaxBytesReader` per request, with a larger cap for known
  bulk routes (git push POSTs) selected by path.
- `ReadHeaderTimeout` bounds slowloris; leave Read/WriteTimeout unset
  — SSE streams and large uploads are legitimately long-lived, and
  the connection cap bounds the damage.

## Raw TCP relays — one magic byte discriminates streams

The same tunnel can carry raw TCP (edge port 22 → node's local sshd).
Because the GATEWAY opens every stream, it alone decides a stream's
kind, and the HTTP path pays zero extra wire bytes: an HTTP stream
starts with the request's method token as always; a relay stream
starts with magic byte `0x00` (no HTTP request can begin with NUL),
then one JSON header line `{"v":1,"port":4222}\n`, then raw bytes both
ways. The node wraps the yamux listener in a demux: peek ONE byte of
each accepted stream (in a per-stream goroutine so a silent stream
can't stall the accept loop, under a deadline), route magic streams to
the relay handler, and hand everything else to the `http.Server`
behind a conn wrapper that replays the peeked byte first — deliver
that byte ALONE from the first Read; chaining another Read could block
on a peer that legitimately paused. Read the header line byte-by-byte
too: an over-reading buffer would swallow relay payload.

SECURITY: the node's configured relay list IS the dial allowlist. The
node never dials `127.0.0.1:<port>` just because the edge asked — the
requested port must match its own config or the stream is closed. A
compromised gateway can therefore reach only the ports the operator
already published. Gateway-side, validate pushed relays (port range,
duplicate edge ports, list cap) and refuse edge ports that collide
with the gateway's own four listeners.

Splice bidirectionally with half-close propagation: EOF from one side
becomes `CloseWrite` on the other (a yamux stream's `Close` is a
write half-close; TCP conns expose `CloseWrite`), so protocols that
shut one direction first (SSH, git) drain cleanly. Wait for BOTH
directions before returning, and run an idle watchdog that kills both
conns after N minutes with no bytes either way — it is also what
unsticks a half-closed pair whose live direction died silently.

## Node-side sync — push on drift, not on schedule alone

A periodic sweep (tens of seconds, plus a "kick" channel admin
mutations poke for immediacy; single-flight via a distributed lock
when replicated) keeps every gateway current:

1. Build the desired config push; hash its content.
2. Poll `GET /v1/status`. Unreachable is NOT an error — next sweep
   retries. The status answers three questions: reachable? which
   serial? which certificates survived to RAM?
3. Re-push config when the hash changed; bump the serial only then.
4. Re-push any certificate the status says is absent from RAM or has
   a stale NotAfter — this is how a gateway restart heals without the
   gateway ever persisting a key.

## Testing the relay

- Build the paired fixture by driving the REAL interactive setup flow
  over piped stdio (`strings.NewReader` answers, `bytes.Buffer`
  output, parse the printed completion blob) — the test then covers
  pairing and the server in one motion.
- Full-stack plumbing test: bind the tunnel listener on `:0` with
  real TLS, run the real node dialer pool against it, front the edge
  handler with `httptest.Server`, and assert (a) a GET round-trips
  with X-Forwarded-* set, (b) an SSE event crosses the edge WHILE the
  handler is still blocked — gate the handler's second event on the
  client having read the first. That one assertion proves the
  flush-aware copy. Poll for "pool up" with a deadline loop, never
  sleep (testing.md).
- Trust-boundary test: after a config push AND a cert push, walk the
  gateway data dir and grep every file for the PEM body's base64
  payload as a canary — it must appear nowhere. Restart the server on
  the same dir: routing and the offline page come back, the
  certificate does NOT, and the status self-report says so.
- Rejection tests: a hello signed by a foreign key must be refused
  and the socket closed; relay streams naming unlisted ports must be
  refused node-side.
- Limiter tests inject the clock (`nowFn func() time.Time` field) —
  token refill is arithmetic, not real waiting.

## Rules

- The node dials out for EVERYTHING — tunnel, config push, status.
  The gateway never dials and never stores the node's address; that
  asymmetry is the NAT traversal and half the security model.
- Keyless-by-construction beats keyless-by-policy: put certificate
  material in a separate message type from config so the config cache
  CANNOT leak a key, rather than remembering to strip one.
- One stream, one exchange (`DisableKeepAlives`); never pool tunnel
  streams for HTTP reuse — the stream's lifetime is your request
  cancellation signal.
- Overwrite X-Forwarded-*, never append; trust them only behind an
  unforgeable in-process marker on the tunnel-served handler.
- Any line-oriented handshake that precedes a binary protocol must be
  read byte-by-byte (or the buffered remainder spliced back in) —
  over-reading swallows yamux/relay frames. Same bug, three places.
- Fail fast when the node is offline: 503 + Retry-After for HTTP,
  close for raw TCP. Never queue requests on the gateway — memory on
  the public box is the attack surface.
- Jitter every reconnect backoff; N independent slots, no shared
  state, so one bad connection never drains the pool.
- Rate-limit state must be bounded (map cap + behavior-neutral sweep)
  or the limiter itself becomes the memory DoS.
