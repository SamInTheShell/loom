# Go

Go knowledge: general engineering practice first, then the systems
library — the building blocks of a databox-style distributed
key-value + blob store and the personal-cloud platform that rides on
it, written so the parts can be assembled cohesively in different
configurations.

General practice:

- **testing.md** — reliable testing in Go: deterministic tests, the
  race detector, injected clocks, in-process cluster harnesses. Read
  this before writing any of the systems below; they are only
  trustworthy if tested this way.
- **urfave-cli-v3.md** — multi-command CLIs with urfave/cli v3:
  the Command tree, flags with env sources, signal-aware contexts,
  and the v3-is-not-v2 differences worth memorizing.

Web applications:

- **webapps/** — server-rendered web apps layer by layer:
  gorilla/mux routing + apache-style access logging, `html/template`
  with embedded layouts, and the kernel/apps/domain MVC monolith
  structure. Start with its README.

Distributed systems:

- **distributed-kv/** — the component catalog for a distributed KV
  and blob store: raft replication (single and multi-group), the
  metadata group, sharding and rebalancing, storage engines (Pebble,
  BadgerDB), erasure coding, blob management, user systems, wire
  protocols, and admin/user frontends. Start with its README — it is
  the assembly map.

Self-hosted service patterns (a private node behind NAT plus tiny
always-public relays):

- **pairing-crypto.md** — the crypto substrate the relay patterns
  share: ed25519 signed requests, X25519/HKDF/ChaCha20-Poly1305
  sealed envelopes, pinned self-signed TLS, the pasted-blob pairing
  handshake, and house conventions (argon2id, TOTP, cert shapes).
- **gateway-relay.md** — public HTTP(S)/TCP ingress for a NATed
  node: yamux reverse tunnels, RAM-only cert custody, node-side
  ACME, per-tenant limits.
- **postoffice.md** — the same split applied to mail: a public SMTP
  relay spooling for a private mail server, with SPF/DKIM/DMARC
  hygiene and the DNS records that make residential mail deliverable.
- **git-hosting/** — embedding a complete git forge: smart HTTP/SSH
  wire protocols, go-git as an in-process engine over KV storage,
  and forge features (issues, merges, permissions, GC). Start with
  its README.
- **protocols.md** — the protocols atlas: every wire the whole stack
  speaks (custom framings, mail, git, S3, pg-wire, SSE, ACME, …),
  each with transport, framing, and auth, linking to the deep dives.

Conventions for Go projects: modules via `go.mod` (`go mod init`,
`go get <pkg>@latest` to add a dependency — never hand-edit require
lines), `gofmt` is not optional, and errors are values — return them,
wrap with `fmt.Errorf("doing x: %w", err)`, and handle them at the
level that can act on them.
