# Protocols atlas - every wire in a personal-cloud + KV stack

A self-hosted personal cloud riding a distributed KV store ends up
speaking a lot of protocols: its own cluster RPC, custom relay planes
to rented edge gateways, and a stack of standard internet protocols so
stock clients (browsers, git, psql, S3 SDKs, other MTAs) work
unmodified. This file is the map - for each protocol: what it is for,
what carries it, the framing in a few precise lines, and how it
authenticates - with links to the deep dives. Read it before inventing
a new wire: most needs are covered by one of these, and anything new
should copy the house conventions (magic prefixes, in-band versions,
signed hellos, one shared vocabulary package) rather than improvise.

## Cluster-internal - the KV platform

Design discussion (gRPC vs custom TCP, framing rules, idempotence)
lives in [distributed-kv/wire-protocols.md](distributed-kv/wire-protocols.md);
this section is what a working system actually runs.

**Client API** - the one public surface for KV, blobs, transactions,
locks, and watches. Transport: HTTPS (TLS 1.3 minimum) with JSON
bodies; verbs on `/api/v1/kv/<key>`, `/api/v1/list`,
`/api/v1/blobs/<key>` (PUT/GET/HEAD/PATCH/DELETE, range reads),
`/api/v1/tx/commit`, `/api/v1/locks/*`, `/api/v1/watch`. Auth: `POST
/api/v1/auth/login` (username/password) returns a session token sent
as `Authorization: Bearer <token>` on every call. Server trust is
pin-or-pool: a CA pool when configured, otherwise explicit SHA-256
certificate fingerprints from a trust-on-first-use store - never a
skip-verification switch. A watch is one long-lived GET whose response
body is a stream of JSON objects (`json.Decoder` loop); a mid-stream
`{"error": ...}` line terminates the watch and is never delivered as
an event. Structured, typed errors drive the client retry convention
(backoff+jitter on Conflict/ShardSplitting; TxTooOld and
RevisionCompacted are NOT retryable as-is).

**Raft transport** - node-to-node consensus messages ride the same
HTTPS server as everything else: one marshaled `raftpb.Message` per
`POST /internal/raft?gid=<group>`. Auth: mutual TLS under the cluster
CA plus a node pre-shared key in a header. Delivery is best-effort by
design - a failed POST reports unreachable to raft and is forgotten;
each peer gets a small send queue so one slow peer can't stall the
rest. Snapshots are the exception: the raft message carries only a
manifest and the bulk data streams over a dedicated
`POST /internal/raftsnap` request.

## The gateway crypto substrate - sealing and signing

Every custom relay protocol below rides one small application-layer
crypto core that sits ABOVE pinned TLS, so compromising TLS alone
never yields plaintext or forgeable requests. Full treatment:
[pairing-crypto.md](pairing-crypto.md).

**Sealed envelope** - payload secrecy to a recipient's key. Format:

```
"PCPS1" (5B magic) | ephemeral X25519 pub (32B) | nonce (12B)
                   | ChaCha20-Poly1305 ciphertext+tag
```

Fresh ephemeral key per message; message key =
HKDF-SHA256(ECDH shared, salt = ephemeral-pub‖recipient-pub,
info = `"pcp-wire-seal-v1"`); the magic is the AAD. Binding both
public keys into the salt means a transplanted envelope can't decrypt.

**Signed request** - request authenticity for every control-plane
call. One header:

```
X-PCP-Auth: v1;ts=<unix>;nonce=<b64url 16B>;sig=<b64url ed25519>
```

The signature covers `context\nMETHOD\npath\nts\nnonce\n`
`hex(sha256(body))` with a domain-separation context string. The
verifier enforces ±5 min clock skew and a nonce replay cache that only
needs to remember one skew window (older timestamps are refused
outright).

**Pairing blobs** - the copy-paste handshake that establishes keys and
endpoints between the home server and a gateway. Format: an ASCII
prefix, then RawURL-base64 of a JSON struct carrying a `v` version
field plus public keys, a one-time pairing token, and (direction
depending) the pinned TLS fingerprint and dial endpoints. Each gateway
kind gets its OWN prefix pair (e.g. `PCPCF1.`/`PCPCF2.` for the edge
ferry, `PCPPO1.`/`PCPPO2.` for mail, `PCPBR1.`/`PCPBR2.` for build
runners) so pasting a code into the wrong tool fails instantly with a
clear message.

## Relay planes - paired gateways

Architecture and pairing lifecycle: [gateway-relay.md](gateway-relay.md);
mail specifics: [postoffice.md](postoffice.md). The home server ALWAYS
dials out - gateways never learn its address - and authenticates the
gateway by the leaf-certificate SHA-256 fingerprint pinned at pairing
(chain and name checks are meaningless against a hand-exchanged
self-signed cert; the pin IS the trust decision).

**Mail control plane** - HTTPS + JSON to the mail gateway, every
request signed with the pairing's control key: `GET /v1/status`,
`PUT /v1/config` (a sealed, serial-versioned ConfigPush that FULLY
replaces gateway state - drift is impossible), `GET /v1/inbound`
(long-poll batch of sealed envelopes, oldest first) +
`POST /v1/inbound/ack`, `POST /v1/outbound` (sealed batch),
`GET /v1/events?cursor=` (delivery outcomes). Secrets (recipient
lists, DKIM private keys) travel only inside sealed payloads and live
in gateway RAM.

**Edge (ferry) control plane** - same substrate, different
vocabulary: the config push carries public hostnames with per-name TLS
mode (`acme|selfsigned|custom`) and pushed RAM-only certificates,
concrete edge limits (the home server resolves defaults - the gateway
never guesses), and the TCP relay table.

**HTTP tunnel** - the data plane. The home server dials the gateway's
tunnel port over pinned TLS and writes ONE newline-delimited JSON
hello `{"v":1,"auth":...}` where auth is a signed request over the
fixed pair `TUNNEL /v1/tunnel` (nil body) - same verifier, same
replay cache as the control plane. The gateway answers one JSON line
`{"ok":...}`; a connection that says nothing inside the handshake
deadline is dropped. Then the raw connection becomes a
`hashicorp/yamux` session - the accepting gateway is the yamux Server
and opens one stream per public HTTP exchange; HTTP bytes are
UNCHANGED on the wire. A pool of sessions round-robins for capacity.
Requests arriving through the tunnel are context-marked so
`X-Forwarded-For/Proto` is trusted from paired gateways only - a
marker no header can forge.

**TCP relay streams** - generic port relays (public edge port →
localhost port at home; SSH is the use case, the code is bytes-only)
share the tunnel. Discrimination is one byte: the gateway opens every
stream, and a relay stream starts with magic `0x00` - no HTTP method
token can begin with NUL - followed by one JSON header line (bounded
at 128 bytes), then raw spliced bytes. HTTP streams carry zero added
bytes; the home-side dialer peeks the first byte to route.

**Buildwire** - the build-runner plane inverts the dial direction
(runners sit behind firewalls, so the RUNNER dials the home server)
but keeps the shape: line-JSON hellos in BOTH directions, each signed
over its own fixed method/path (`HELLO /buildwire/hello` from the
runner, `HELLO /buildwire/hello-reply` back) so both sides prove
identity before yamux starts. Then one yamux session with the home
server as yamux server: it opens control streams (config, dispatch,
cancel) while the runner opens report streams (phase/step/build
status, log chunks, artifact up/downloads). Each stream carries one
framed message - a JSON header line naming the type and payload
length, then the payload bytes: JSON for statuses and config, raw
bytes for logs and artifacts. Build secrets ride sealed to the
runner's key; the dispatcher only ever holds ciphertext.

## Standard internet protocols implemented

**SMTP, inbound** - the gateway is a real public MTA (built on
`emersion/go-smtp`, STARTTLS offered) whose every policy input comes
from the config push: connection gates (global/per-IP concurrency,
per-IP rate) before any verb does work; `MAIL` checks SIZE and spool
room (452 tempfail - senders retry for days, mail is never dropped);
`RCPT` checks a SALTED-HASH recipient manifest and answers 550 for
unknowns - reject-at-RCPT, never accept-then-bounce (the backscatter
guard); `DATA` is cap-enforced, a Received line is stamped, and the
whole envelope is sealed to the home server's key before the spool
write. No config yet → 421 for everything: refusing mail honestly
beats spooling mail you can't route.

**SMTP, outbound** - the gateway DKIM-signs with per-domain keys from
the config push, resolves MX records in priority order (with the RFC
5321 §5.1 implicit-MX fallback to the domain itself), and delivers
with opportunistic STARTTLS - encrypted first, plain fallback, no
certificate validation, which is the inter-MTA norm. The home server's
outbound queue stays authoritative: a message is queued until a `sent`
event arrives on `/v1/events`, so a gateway restart just gets it
re-submitted. Deliverability DNS (SPF `v=spf1 … -all` listing every
gateway IP, DKIM TXT per selector, DMARC `v=DMARC1; p=quarantine`,
reverse-DNS rows) is generated for the operator by the admin surface -
see [postoffice.md](postoffice.md).

**Git smart HTTP** - `GET /git/{ns}/{repo}/info/refs?service=…` for
the ref advertisement (with the `# service=…` pkt-line prefix HTTP
requires), `POST …/git-upload-pack` (fetch/clone) and
`POST …/git-receive-pack` (push) with the matching
`application/x-git-*-result` content types. Framing is git pkt-line
and protocol v0 negotiation via `go-git`'s plumbing (`pktline`,
`packp`, `packfile`) - one transport-agnostic engine handles
advertisement, upload negotiation, and the whole push path (locks,
quotas, atomic ref updates, report-status). Transports authenticate
FIRST and hand the engine an authorized (repo, user) pair. Deep dive:
[git-hosting/smart-protocols.md](git-hosting/smart-protocols.md).

**Git over SSH** - an `x/crypto/ssh` server (default `:4222`, or
relayed from edge port 22 through the TCP relay above) speaking
exactly two exec commands, `git-upload-pack` and `git-receive-pack`,
over the SAME engine - SSH runs the interactive bidirectional
negotiation loop the stateless HTTP exchange composes from pieces.
Public-key auth ONLY against a stored fingerprint index (username
`git` or the key owner's own name; identity comes from the key); no
passwords, no anonymous SSH, no pty/forwarding/agent services. Host
key is a cluster-shared ed25519 identity. "No repo" and "no access"
are ONE uniform "repository not found" - a prober with a valid key
learns nothing.

**S3 API (subset)** - a stateless gateway translating core S3 onto
blobs: bucket `b`, object `o` → key `/s3/<b>/<o>`, buckets are
prefixes, objects are blobs, plus list, multipart, and ETags. Two AWS
auth transports are verified: the `Authorization: AWS4-HMAC-SHA256
Credential=…, SignedHeaders=…, Signature=…` header, and presigned
query auth (`X-Amz-Algorithm/-Credential/-Date/-Expires/`
`-SignedHeaders/-Signature`, expiry bounded to 1..604800 s) for
browser download and direct-upload links. Signatures verify against stored access-key
secrets; authorization then evaluates the owning user's grants with
the same resolver the storage core uses.

**SigV4 (client side)** - backup to any S3-compatible endpoint uses a
minimal hand-rolled signer (no AWS SDK): canonical request
`METHOD\npath\nquery\ncanonical-headers\nsigned-names\npayload-hash`
→ string-to-sign → the dated HMAC key chain → hex signature.
Path-style URLs so MinIO/Ceph/self-hosted gateways all work. Signer
and verifier live on opposite sides of the codebase and cross-check
each other in tests - build both if you build either.

**PostgreSQL wire v3** - the SQL layer speaks the pg protocol so any
`psql`/libpq/pgx/psycopg client connects, while the dialect carried
over it is the store's own (the "pg transport, own dialect" model
QuestDB and immudb use). Framing: every typed message is a 1-byte
type, a 4-byte big-endian length that INCLUDES those four bytes, then
the body (the startup message alone is untyped). Both the simple
query protocol (`Q`) and the extended protocol are implemented -
Parse/Bind/Describe/Execute/Close/Sync with real `$N` parameters,
named prepared statements and portals, text format for every type and
binary for fixed-width ones. TLS is offered via SSLRequest; password
auth is verified by logging in to the cluster, so the KV grant model
applies to SQL at table granularity through the key mapping.

**SFTP** - a backup destination over `github.com/pkg/sftp` on
`x/crypto/ssh`; password auth. Host-key policy is trust-on-first-use
per process: first contact records the key and logs its SHA-256
fingerprint for out-of-band verification; a later different key from
the same host is refused.

**ACME** - certificate issuance runs at HOME (`x/crypto/acme`,
HTTP-01): account and cert private keys never leave the home store;
challenge tokens live in the KV store so any replica answers; the
challenge request arrives at the GATEWAY on port 80 and tunnels down
like any other request, so issuance needs nothing on the gateway.
Issued certs are pushed RAM-only; self-signed and operator-uploaded
certs share the same push path.

**ICS / iTIP** - calendar invites are a dependency-free RFC 5545
subset whose generator and parser round-trip each other: one VEVENT
per message, UTC times only (no VTIMEZONE; a foreign TZID parses with
its local time read as UTC - a documented approximation), METHOD
REQUEST/REPLY/CANCEL (RFC 5546), 75-octet line folding, text
escaping. Invites travel as mail parts (iMIP); there is no
CalDAV/CardDAV server.

**OTLP** - tracing exports spans over OTLP/HTTP, configured entirely
by the standard `OTEL_*` environment variables; unset endpoint means
a no-op provider - no goroutines, no network attempts. Context
propagates as W3C `traceparent`/`tracestate` plus baggage.

## Browser-facing

Routing and middleware for this layer:
[webapps/gorilla-mux.md](webapps/gorilla-mux.md).

- **Sessions** - one cookie (e.g. `pcp_session`) whose VALUE keys a
  session record in the replicated store, so any replica serves any
  request. Signed-in mutations run a CSRF check. The Secure flag is
  decided per request: the global setting OR, on tunnel-marked
  requests only, the gateway's word that the public leg was HTTPS. A
  non-HttpOnly theme cookie is the one deliberate exception (the
  toggle script writes it).
- **API keys** - a PARALLEL bearer path for `/api/v1`: `Authorization`
  header only, no cookies, no CSRF, never grants the web UI; routes
  declare required scopes; every failure is one JSON envelope
  `{code, error}` - never HTML inside the API prefix.
- **SSE** - live events are Server-Sent Events: set
  `Content-Type: text/event-stream`, `Cache-Control: no-cache`,
  `X-Accel-Buffering: no`; clear the server's read/write deadlines for
  THIS connection only (the stream is long-lived by design); flush an
  opening comment (`: connected\n\n`) so the client's `onopen` fires;
  flush after every event; cap concurrent streams per user (~12) so
  one browser can't pin unbounded goroutines.
- **WebSockets** - not spoken natively; the edge proxy passes the
  Upgrade through verbatim and splices a 101 response into a raw
  bidirectional byte relay.

## Rules

- Version every format in-band from day one: magic prefixes
  (`PCPS1`), `v` fields in hellos and pairing blobs, `v1;` header
  prefixes, serial-numbered config pushes. Refuse unknown versions
  loudly.
- One DISTINCT magic/prefix per protocol pair - a mis-paste or
  mis-dial must fail instantly and legibly, never half-work.
- One shared vocabulary package per custom protocol, imported by both
  halves, so the two ends can never drift.
- TLS trust is pin-or-pool everywhere; `InsecureSkipVerify` appears
  only with explicit replacement verification (fingerprint pinning)
  on the same connection.
- Gateway traffic layers app crypto ABOVE TLS: seal payloads to the
  recipient's key, sign requests with the sender's key - compromising
  the transport alone yields nothing.
- Signed hello before multiplexing, with a deadline: a dialer that
  connects and says nothing doesn't get to hold a socket.
- Bound every length before allocating or reading: header-line caps,
  frame caps, body caps, per-user stream caps
  ([distributed-kv/wire-protocols.md](distributed-kv/wire-protocols.md)
  has the frame-decoding rules; fuzz every decoder).
- Speak STANDARD protocols at every edge a stock client touches
  (SMTP, git, S3, pg wire, SSH, SSE); keep custom wires for internal
  legs where both binaries ship from one repo.
- Every sync loop is at-least-once with an idempotence anchor:
  config serials skip no-op pushes, event cursors resume, inbound
  acks delete only what was delivered, deterministic ids dedupe
  re-delivery. Design the anchor with the protocol, not after it.
