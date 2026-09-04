# Smart protocols - serving git over HTTP and SSH

Stock `git clone/fetch/push` speaks two wire services - `git-upload-
pack` (fetch) and `git-receive-pack` (push) - over a transport. This
file is how to serve both from a Go process using go-git's protocol
plumbing (packages `plumbing/protocol/packp`, `plumbing/format/
pktline`, `plumbing/transport/server`) against the embedded storer
(embedded-go-git.md), with no `git` binary on the server. Build the
protocol core transport-agnostic, then wrap it twice: a smart-HTTP
handler set and an `x/crypto/ssh` server. Both transports do auth and
authorization first and hand the core an already-authorized
(repo, user) pair; the core never sees credentials.

```
go get github.com/go-git/go-git/v5
go get golang.org/x/crypto/ssh
```

## pkt-line, the framing everything rides

Every protocol message is a pkt-line: 4 ASCII hex digits of total
length (including the 4), then payload. `0000` is the flush-pkt - a
delimiter, not data. go-git does the encoding; you only ever handle
two raw cases yourself:

- **the probe**: before streaming a body larger than its
  `http.postBuffer` (default 1 MiB), git POSTs a lone `0000` to
  confirm auth cheaply. Peek 4 bytes; if `"0000"`, answer 200 with
  the result content-type and an empty body. This applies to BOTH
  POST endpoints. Over SSH the same peek detects "nothing to push" /
  ls-remote hanging up after the advertisement - treat as success.
- **have lines**: go-git's `UploadRequest.Decode` stops at the
  want-list flush; the `have <sha>` lines and the `done` marker that
  follow are read with `pktline.NewScanner` yourself (below).

## The HTTP endpoints (protocol v0, stateless-rpc)

```
GET  /{ns}/{repo}/info/refs?service=git-upload-pack    advertisement
GET  /{ns}/{repo}/info/refs?service=git-receive-pack   advertisement
POST /{ns}/{repo}/git-upload-pack                      fetch rounds
POST /{ns}/{repo}/git-receive-pack                     push
```

Strip a trailing `.git` from the repo segment. Any other `service`
value (or a bare `info/refs` - the dumb protocol) is 404. Response
headers, always: `Cache-Control: no-cache`, and content-type
`application/x-git-upload-pack-advertisement` (per service) on
info/refs, `application/x-git-upload-pack-result` /
`application/x-git-receive-pack-result` on the POSTs. Wrap the
response writer so every write is followed by `http.Flusher.Flush()`
- pack data must stream, not buffer.

Request bodies may arrive gzipped: if `Content-Encoding: gzip`, wrap
in `gzip.NewReader`. Cap bodies with `http.MaxBytesReader` - a
negotiation body (wants+haves) needs ~16 MiB at most; the push body
cap is your policy.

### Advertisement

Use go-git's server with a loader that returns your storer:

```go
type fixedLoader struct{ sto storer.Storer }
func (l fixedLoader) Load(*transport.Endpoint) (storer.Storer, error) {
    return l.sto, nil
}

srv := server.NewServer(fixedLoader{sto})
sess, _ := srv.NewUploadPackSession(&transport.Endpoint{}, nil)
ar, _ := sess.AdvertisedReferencesContext(ctx)   // *packp.AdvRefs
// smart HTTP requires a service preamble; SSH sends none:
ar.Prefix = [][]byte{[]byte("# service=git-upload-pack"), pktline.Flush}
ar.Encode(w)
```

Receive uses `NewReceivePackSession` identically. The advertisement
lists refs + capabilities; whatever it offers is what you must later
accept - reject any client capability it didn't advertise, before
writing anything.

### Upload-pack: one stateless round per POST

The v0 HTTP exchange is: client POSTs wants + a batch of haves; you
answer ACK/NAK (no pack) while negotiation continues, and the final
ACK/NAK + pack once the client says `done`. Each POST is independent
- decode everything from the body, keep no session state:

```go
upreq := packp.NewUploadPackRequest()
upreq.UploadRequest.Decode(body)          // stops at the want flush
haves, done, err := readHaves(body)       // your scanner loop:
//   "have <40-hex>" → collect; "" (flush) → keep going;
//   "done" → return done=true; anything else → 400
common := filterKnown(sto, haves)         // HasEncodedObject == nil
if !done {                                // negotiation continues
    resp := packp.ServerResponse{}
    if len(common) > 0 { resp.ACKs = common[:1] }  // single-ack
    resp.Encode(w, false)                 // NAK when no ACKs
    return
}
upreq.Haves = common                      // only KNOWN haves -
// revlist errors on unknown hashes, and the pack must contain
// everything the client doesn't provably have
resp, _ := sess.UploadPack(ctx, upreq)    // computes + streams pack
if len(common) > 0 { resp.ServerResponse.ACKs = common[len(common)-1:] }
resp.Encode(w)
```

Single-ack (no `multi_ack` capability) means: ACK the FIRST common
object once per response, NAK otherwise; the final response ACKs the
last common (or NAK). It costs some pack size on partial fetches and
saves a state machine - the right v1 trade.

### Receive-pack: the push path

The shared core (called by HTTP and SSH alike) takes a decoded
`packp.ReferenceUpdateRequest` and does, in order:

1. **Capability gate** - allow exactly what the advertisement
   offered: `agent`, `ofs-delta`, `delete-refs`, `report-status`.
   Anything else: error before any output.
2. **Per-repo push lock** - the same lock GC holds (embedded-go-git
   .md), so a push and a collection never interleave. Busy → HTTP
   503 / SSH stderr, retryable.
3. **Quota pre-charge** - when the transport knows an upper bound
   (plain HTTP `Content-Length`, not gzipped), charge it up front;
   otherwise (gzip, chunked, all of SSH) charge incrementally from
   the storer's stored-bytes hook, then reconcile to actual bytes
   after the unpack. Refund everything on failure.
4. **Unpack** - `packfile.UpdateObjectStorage(sto, req.Packfile)`
   expands the pack to loose objects through your storer (which
   enforces per-object size and object-count caps), then `Flush`.
5. **Verify + atomic ref update** - every non-delete command's
   `cmd.New` must now exist in the store; then apply ALL commands in
   one compare-and-swap transaction against `cmd.Old` (git's atomic
   multi-ref push). A stale old value rejects the whole push
   ("fetch first").
6. **Hooks** - after success: note the ref updates for debounced GC,
   and refresh open merge-request heads for moved branches
   (forge-features.md). Best-effort, after the push already landed.
7. **report-status** - when the client asked for it (stock git
   always does): `packp.NewReportStatus()`, `UnpackStatus: "ok"` or
   a terse error, one `CommandStatus` per command - all "ok" or all
   the same error, because the push is atomic.

Error contract that keeps transports simple: an error RETURN means
nothing was written (the transport renders it - HTTP status, SSH
stderr); once the unpack starts, failures land inside the
report-status stream and the handler returns success. Collapse
internal errors to terse messages on the wire ("push rejected");
only quota/cap/stale errors keep their text.

### HTTP auth

git sends Basic auth. Map it onto API tokens, not browser sessions:
username + token-as-password, verified for a `git:read` or
`git:write` scope, then the repo role check (forge-features.md).
Rules that prevent information leaks:

- anonymous (no credential) may fetch PUBLIC repos only; anonymous
  push never - 401 with `WWW-Authenticate: Basic realm="…"`.
- bad credential → 401 challenge. Authenticated but lacking
  role/scope, or repo nonexistent → 404. Never 403: a prober must
  not distinguish "exists, forbidden" from "doesn't exist".

## The SSH transport

An `x/crypto/ssh` server on its own port that speaks exactly two
exec commands. Everything a login host would offer is refused.

**Server config**: `PublicKeyCallback` only - no passwords, no
anonymous. Generate an ed25519 host key ONCE, persist it (PKCS8 PEM)
under a fixed storage key claimed with an OCC/CAS write so every
replica presents one identity, and log its
`ssh.FingerprintSHA256` at startup.

**Auth**: index registered public keys by hex SHA-256 of the
wire-format key (`sha256(pub.Marshal())` hex - base64 fingerprints
contain `/` and break key-prefix storage) mapping to (owner, keyID).
The callback: look up the fingerprint, check the owner exists and
isn't banned, and accept login name `git` (the forge convention -
identity comes from the key) or the owner's own username. EVERY
failure returns the same "permission denied" after spending a
per-IP failure token (token bucket, ~10 failures/minute; successes
are free). On success stash the username in
`ssh.Permissions.Extensions` and touch the key's last-used stamp
(throttled) on a background goroutine.

When users register keys: parse with `ssh.ParseAuthorizedKey`,
accept ed25519, ECDSA (nistp256/384/521), RSA ≥ 3072 bits; reject
certificates and sk-* types. Claim the fingerprint index row in the
same transaction as the key record so one key maps to exactly one
account.

**Connections**: cap concurrent conns per IP; wrap the `net.Conn` so
every read AND write pushes the deadline forward (idle timeout ~10
min - long pack streams keep refreshing it); `ssh.DiscardRequests`
on global requests; reject non-`session` channels
(`direct-tcpip`, x11, agent) with `ssh.Prohibited`; allow ONE
session channel per connection.

**Session requests**:

- `exec` - reply true, run the command, send `exit-status`
  (`ssh.Marshal(struct{ Status uint32 }{code})`), close.
- `shell` - reply true, print a "successfully authenticated, no
  shell access" banner to stderr, exit 1. (This is what
  `ssh git@host` tests.)
- everything else (`pty-req`, `env`, `subsystem`, forwarding) -
  reply false.

**Command parsing**: stock git sends the hyphenated single-quoted
form `git-upload-pack '/ns/repo.git'`; some clients send
`git upload-pack path`. Tokenize with openssh quoting rules (single
quotes literal, double quotes honor backslash), normalize
`git upload-pack` → `git-upload-pack`, and whitelist exactly the two
services - anything else is a one-line stderr refusal, exit 1.
Resolve the path (`/ns/repo`, optional `.git`), then the role check:
upload needs read, receive needs write, and nonexistent and
no-access both answer the same `repository not found` on stderr.

**Serving**: write the advertisement (no HTTP prefix) to the
channel, then:

- upload-pack runs the INTERACTIVE loop (SSH is bidirectional, no
  stateless rounds): read the `UploadPackRequest`, then scan
  pkt-lines - collect `have`s per batch, on each flush answer one
  `packp.ServerResponse` (ACK the first common once, else NAK), on
  `done` filter haves to known and stream the final ACK/NAK + pack
  exactly like the HTTP final round. A client that disconnects
  mid-negotiation is success, not an error.
- receive-pack peeks for the lone flush (nothing to push → exit 0),
  decodes the `ReferenceUpdateRequest`, and calls the same shared
  receive core with no pre-charge (SSH never knows a length -
  incremental accrual). Pre-report errors print to stderr; once the
  report-status stream started, the core already answered.

Bound one exec end-to-end with a generous context timeout (~60 min
- big clones are legitimate), and re-check any "service enabled"
switch per command, not per connection.

## Rules

- One protocol core, N transports. The moment HTTP and SSH have
  separate receive paths, their quota/lock/hook behavior drifts.
- Never write protocol output before the last pre-check; after the
  first byte the only error channel is the protocol's own
  (report-status, stderr). Mixing them corrupts the stream.
- Filter haves to objects you actually have BEFORE `UploadPack` -
  go-git's revlist errors on unknown hashes.
- Answer the probe flush-pkt on both POST endpoints and on SSH; a
  server that 400s the probe breaks every push over 1 MiB.
- 404 (HTTP) / "repository not found" (SSH) for both nonexistent
  and forbidden; 401/"permission denied" for every auth failure
  identically. Distinct answers are an oracle for private repos.
- Rate-limit SSH auth FAILURES per IP and cap conns per IP - an SSH
  port is a brute-force magnet; successes must stay free or a busy
  CI clone loop locks itself out.
- Test with the real `git` CLI against a served temp repo (clone,
  push, force-push, delete-branch, ls-remote, an over-postBuffer
  push for the probe). go-git-as-client tests miss stock git's
  exact byte behavior (../testing.md).
