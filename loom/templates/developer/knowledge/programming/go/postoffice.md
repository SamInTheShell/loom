# Postoffice — a public mail relay for a self-hosted private server

Running mail from a residence fails for two reasons: inbound needs a
stable public IP with port 25 open (and to be up when senders retry),
and outbound from residential IP space is rejected outright by the
big receivers. The postoffice pattern fixes both with a tiny always-on
relay on a cheap cloud VM: it terminates SMTP for your domains, spam-
scores, and spools inbound mail until the private node collects it;
and it relays the node's outbound mail from a clean IP with full
DKIM/SPF/DMARC hygiene. The node stays invisible — it always dials
the relay; the relay never learns the node's address.

The trust boundary is the whole point: the relay box is assumed
stealable. Its disk holds only its own identity keys, the node's
PUBLIC keys, salted hashes of valid addresses, and spool files sealed
to a key it does not possess. Mail plaintext, recipient addresses,
and DKIM private keys never rest on the relay. Pairing, request
signing, payload sealing, and TLS pinning are the shared unit in
pairing-crypto.md — this doc only says where each is applied. The
same split (dumb public box, authoritative private node, pasted-blob
pairing) generalizes to non-mail traffic in gateway-relay.md.

```
go get github.com/emersion/go-smtp     // SMTP server + client
go get github.com/emersion/go-msgauth  // dkim, dmarc
go get blitiri.com.ar/go/spf           // SPF check
go get github.com/emersion/go-message  // parsing, node side
```

## Architecture

The relay is one static binary with two subcommands: `setup` (the
pairing flow) and `run` (serve). It listens on two ports: `:25` SMTP
for the world, and `:8443` HTTPS — a control plane with six routes
the node dials, every request signed, server cert pinned
(pairing-crypto.md):

```
GET  /v1/status        health/self-report poll
PUT  /v1/config        sealed config push (declarative, replaces all)
GET  /v1/inbound       long-poll spool drain (?wait=25s, cap 55s)
POST /v1/inbound/ack   delete delivered spool entries
POST /v1/outbound      sealed batch of messages to deliver
GET  /v1/events        delivery outcomes past ?cursor=
```

Authority flows node → relay. There is nothing to configure on the
relay beyond listen addresses: domains, recipients, DKIM keys,
limits, spam policy all arrive in the config push. Bound one control
body at 64 MiB.

## Config push — declarative, RAM vs disk split

The push carries everything: a `ManifestSerial`, the public hostname
(HELO/Received identity — admin-correctable without re-pairing),
every deliverable address flattened, per-domain DKIM selector +
private key PEM, DNSBL zones, spamd address + tag/reject scores, and
ALL limits as concrete numbers (max message bytes, max rcpt, max
conns global/per-IP, per-IP messages/minute, spool cap bytes,
per-recipient share %). A push fully replaces the previous state —
drift is impossible.

On apply the relay splits it:

- **RAM**: the full push, DKIM keys included.
- **Disk** (`config.json`): only what standalone operation needs and
  theft can't use — a fresh random 16-byte salt plus
  `hex(HMAC-SHA256(salt, lowercase(addr)))` per recipient, domain
  NAMES, policy numbers, the serial. Plaintext recipients are nilled
  before the RAM copy outlives the frame.

A restart therefore loses DKIM keys on purpose. The node's sync loop
(every ~20s) polls `/v1/status`, which reports the applied serial and
a `dkim_in_ram` flag; it re-pushes whenever the content hash changes
(sha256 of the JSON with serial zeroed) OR the flag says a restart
dropped the keys — even if serials match. Until the first-ever push,
the SMTP server answers 421 to everything: refusing mail honestly
beats spooling mail you can't route.

## Inbound path (SMTP → sealed spool)

Build on `emersion/go-smtp` (`smtp.NewServer(backend)`, 2-minute
read/write timeouts, SMTPUTF8 on; leave the library's size/rcpt caps
at 0 and enforce per-config yourself). Gate in this order:

**Connect** — global and per-IP concurrency counters (421 over
limit), then DNSBL: reverse the IPv4 octets and look up
`d.c.b.a.<zone>` for each configured zone; any answer = 554 with the
zone named. Fail OPEN — an unreachable zone never blocks mail.

**MAIL** — per-IP token bucket (N msgs/minute, refill continuous,
bucket map capped ~10k with opportunistic eviction of full buckets)
→ 451 "slow down"; declared SIZE over cap → 552; spool byte cap
would overflow → 452 tempfail (senders retry for days; mail is
delayed, never lost).

**RCPT** — max recipients → 452; then the manifest check: hash the
address with the CURRENT salt and look it up. Unknown → 550 "no such
recipient" AT RCPT TIME — never accept-then-bounce, which makes you
a backscatter source. Then the per-recipient spool share (a single
inbox flooding can't starve the rest; shares are RAM-only —
persisting per-recipient byte counts would leak who gets mail).

**DATA** — read through `io.LimitReader(r, max+1)`, 552 over.
Then authenticate the message:

- SPF: `spf.CheckHostWithSender(ip, domainOf(mailFrom), mailFrom)`.
- DKIM: `dkim.Verify(bytes.NewReader(raw))` — pass if any signature
  verifies.
- DMARC: `dmarc.Lookup(headerFromDomain)`; aligned = SPF pass OR
  DKIM pass; failing alignment under `p=reject` → 550 refuse here.
  All lookups fail open — a DNS blip must not bounce real mail.

Stamp `Authentication-Results: <hostname>; spf=…; dkim=…; dmarc=…`,
then spam-score via spamd (SpamAssassin's SPAMC protocol over TCP —
`CHECK SPAMC/1.2\r\nContent-length: N\r\n\r\n<msg>`, parse the
`Spam: True ; 6.1 / 5.0` reply line; score 0 on any error). Score ≥
reject threshold → 554; otherwise prepend `X-Spam-Score:` and let
the node route tagged mail to Spam. Finally prepend your own
`Received:` line (first header — and the only trace hop the system
ever shows), JSON-encode the envelope
`{from, rcpts, received_at, remote_ip, spam_score, raw}`, SEAL it to
the node's X25519 key, and write to the spool. Only ciphertext ever
touches disk.

## Spool and the drain protocol

One file per message: `<%019d unixnano>-<8 hex rand>.sealed`, written
tmp-then-rename, 0600. IDs are time-sortable, so a plain string sort
is the drain order and restart recovery is a directory scan (sizes
and ids only — content is opaque). `Wait(d)` parks long-pollers on a
channel closed by the next Put.

The node drains: `GET /v1/inbound?wait=20s` → batch of ≤32 messages /
≤8 MiB oldest-first plus `more`; unseal each, parse, deliver into
mailboxes IDEMPOTENTLY (message id derived from relay-id + spool-id +
mailbox, thread id from content — a crash between deliver and ack
re-delivers into the same ids, nothing duplicates), then
`POST /v1/inbound/ack` deletes them. Two poison-pill rules: an entry
sealed to a key you no longer hold (re-pair happened) is
undeliverable forever — ack it away rather than wedge the queue; an
over-quota mailbox gets a DSN queued outbound and the entry acked,
never left to block the drain. Skip the DSN when the envelope sender
is the null path — bouncing a bounce is how mail loops are born.

## Outbound path (node → relay → MX)

The node submits sealed batches: `{out_id, mail_from, rcpt_to, raw}`.
`out_id` is the idempotency key — the relay's enqueue silently drops
duplicates. The relay queue is **boot-ephemeral**: entries are sealed
to a keypair minted at process start and never persisted, so a crash
leaves unreadable bytes. That's safe because the NODE's queue is
authoritative: a row stays `submitted` until a `sent` event arrives,
and gets re-submitted (same out_id) when it doesn't.

Per delivery attempt (loop ticks ~30s over due items):

1. **Strip the boundary headers.** Remove, case-insensitively, every
   trace/originating header before the message leaves: `Received`,
   `Return-Path`, `Authentication-Results`, `Received-SPF`,
   `X-Spam-Score`, `X-Originating-IP`, `X-Mailer`, `User-Agent`,
   `X-Forwarded-For`, and `Bcc` (never transmit it). Keep folded
   continuations attached to their header when filtering. Prepend
   ONE `Received: by <gateway> …` line — the recipient sees a single
   clean hop, nothing that names the private node. Leave body bytes
   untouched so an upstream DKIM signature survives a forward.

2. **DKIM-sign** with the sender domain's key from the last push
   (`emersion/go-msgauth/dkim`): parse the PKCS#8 PEM to a
   `crypto.Signer`, `dkim.Sign` with Domain, the pushed Selector,
   `crypto.SHA256`, HeaderKeys `From, To, Cc, Subject, Date,
   Message-ID, MIME-Version, Content-Type`. No key in RAM yet (fresh
   boot, pre-push) or sign error → deliver UNSIGNED rather than not
   at all.

3. **Deliver per recipient domain.** `net.LookupMX`, stable-sort by
   preference, fall back to the domain itself (implicit MX, RFC 5321
   §5.1). Dial each MX on :25 preferring its IPv4 address —
   receivers reject IPv6 sources without confirmed PTR far more
   often. Opportunistic STARTTLS first
   (`smtp.DialStartTLS(addr, &tls.Config{InsecureSkipVerify: true})`
   — encryption without validation is the inter-MTA norm), plain
   `smtp.Dial` fallback. HELO with the pushed hostname. Across MX
   hosts keep the MOST INFORMATIVE error for the bounce: 5xx reply >
   4xx > connection failure > NXDOMAIN.

4. **Classify and retry.** Permanent = SMTP 5xx or DNS "no MX
   exists"; everything else (4xx, refused, timeout, temp DNS) is
   temporary. All recipients permanent-failed → bounce NOW with the
   collected reasons. Any temp failure → defer: backoff
   `attempts × 15min` capped at 6h, give up after 72 attempts (~3
   days) → bounce. Success → `sent`.

Outcomes append to an in-RAM event ring (seq-numbered, capped 5000);
the node polls `/v1/events?cursor=` and applies them: `sent` clears
the queue row and its blob, `bounced` flags the Sent copy and
materializes a DSN in the sender's inbox, `deferred` is
informational. Cursors persist node-side per relay.

## DNS the operator must publish

The node builds the record sheet from what it actually knows — the
relay endpoints and the public IPs each relay self-reports in status
(enumerate global-unicast, non-private addresses of up interfaces:
v4 AND v6, because mail may leave over either):

- **MX** per relay serving the domain, preferences 10, 20, … in the
  domain's relay-priority order (outbound tries the same order).
- **SPF** — TXT on the domain listing every relay's REAL sending
  IPs: `v=spf1 ip4:A ip6:B … -all` (fall back to `a:<host>` until
  first status poll). Because outbound MAIL FROM uses the domain the
  relay serves and leaves from these IPs, SPF aligns for DMARC.
- **DKIM** — TXT at `<selector>._domainkey.<domain>`:
  `v=DKIM1; k=rsa; p=<base64 SPKI>`. Mint RSA-2048 per domain (not
  ed25519 — RFC 8463 verification is far from universal), private
  key PKCS#8 PEM held on the node and pushed to relays only. One
  fixed selector (e.g. `pcp`) keeps the sheet simple; rotation mints
  `pcp2`.
- **DMARC** — TXT at `_dmarc.<domain>`:
  `v=DMARC1; p=quarantine; rua=mailto:postmaster@<domain>` (any
  valid policy the operator prefers passes verification).
- **PTR** per sending IP — forward-confirmed reverse DNS set at the
  hosting provider: the PTR name must resolve back to the same IP.
  Major receivers reject mail from IPs without it.

Verify the sheet live from the node with an injectable resolver, and
render "couldn't look this up from here" (resolver blocked) as
distinct from "missing" — never a false red. Auto-create RFC 2142
`postmaster@` and `abuse@` aliases when a domain is added.

## Rules

- Reject at RCPT, never accept-then-bounce — the relay must not be a
  backscatter source. The one legitimate late bounce (over-quota) is
  a DSN from the node, skipped for null-path senders.
- Fail open on every external dependency (DNSBL, SPF/DKIM/DMARC
  lookups, spamd); fail closed only on identity (signature, seal,
  pinned cert — pairing-crypto.md).
- Spool pressure is 452 tempfail, never 550 and never a drop —
  senders retry for days for free.
- DKIM keys, plaintext addresses, and mail plaintext never rest on
  the relay; prove it with a test that exercises the full path then
  greps every data-dir file for planted secrets (testing.md).
- Ephemeral relay queue + authoritative node queue + idempotency
  keys everywhere: crash-consistency by re-submission and dedup, not
  by fsync ceremony.
- One Received line leaves the system, stamped by the relay; strip
  all other trace headers on the way out, but never touch body bytes.
- Prefer IPv4 for outbound dials, and put every address mail can
  leave from into SPF with working FCrDNS — deliverability is DNS
  work, not code.
- Drop TLS-handshake scanner noise from the exposed control-plane
  logs; an internet-facing 8443 collects probes forever and the
  rejections are correct.
