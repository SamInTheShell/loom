# Pairing & crypto - authenticating a private node to public relays

How a self-hosted private node securely pairs with, and forever after
authenticates to, small public relay boxes it rents (a mail gateway,
a TCP/HTTP relay - postoffice.md and gateway-relay.md both ride this
layer). The design goals: the relay is assumed stealable - nothing on
its disk may decrypt traffic or impersonate the node; the node never
exposes its own address - it always dials OUT; and pairing needs no
PKI, no CA, no shared password - a human copy-pasting two blobs
through the admin UI and an ssh session IS the secure channel.

Everything below x/crypto is the standard library.

```
go get golang.org/x/crypto
```

## Key material - who holds what

Each side of a pairing owns TWO 32-byte keypairs, minted with
`crypto/rand` and stored base64 (std encoding):

- an **ed25519 signing pair** ("control") - authenticates requests;
- an **X25519 sealing pair** - payloads are encrypted TO its public
  half.

```go
func NewSealPair() (privB64, pubB64 string, err error) {
    priv := make([]byte, 32)
    rand.Read(priv)
    pub, err := curve25519.X25519(priv, curve25519.Basepoint)
    ...
}
func NewSignPair() (privB64, pubB64 string, err error) {
    pub, priv, err := ed25519.GenerateKey(rand.Reader)
    ...
}
```

The NODE is the authority: it mints its own control+seal pairs per
relay and keeps the private halves in its database (the same trust
root as password hashes). The RELAY mints its own sign+seal identity
during setup and keeps the private halves in its data dir. Each side
learns only the OTHER's public halves. Validate any key arriving in a
blob with a shape gate (base64 decodes to exactly 32 bytes) before
storing it.

## The pairing handshake - two pasted blobs

Pairing is a token exchange through the operator, not a network
protocol. Sequence:

1. Node UI creates a pending relay record: fresh control+seal pairs
   plus a single-use **pairing token** (24 random bytes,
   base64url - `RandomToken(24)`). It renders a **setup blob**:
   a prefix + base64url(JSON):

   ```
   PCPPO1.eyJ2IjoxLCJuYW1lIjoi...
   ```

   ```go
   type SetupBlob struct {
       V            int    `json:"v"`
       Name         string `json:"name"`
       PCPControl   string `json:"pcp_control_pub"`
       PCPSeal      string `json:"pcp_seal_pub"`
       PairingToken string `json:"pairing_token"`
   }
   ```

   Give each relay KIND its own prefix pair (mail `PCPPO1.`/`PCPPO2.`,
   ferry `PCPCF1.`/`PCPCF2.`): a mis-paste fails instantly with a
   clear error, and the prefix versions the format.

2. Operator ssh-es to the relay and runs `relay setup`, pasting the
   blob and confirming the relay's PUBLIC host:port (reject bind
   wildcards like `0.0.0.0` - the most common mistake). Setup stores
   the node's public keys, mints the relay's own sign+seal identity,
   and self-signs a TLS certificate (below). It prints a
   **completion blob** (`PCPPO2.` + base64url JSON): the relay's two
   public keys, the sha256-hex fingerprint of its TLS leaf cert (DER),
   the confirmed endpoint, and the pairing token ECHOED BACK.

3. Operator pastes the completion blob into the node UI. The node
   verifies: record still pending, token matches EXACTLY (this is
   what proves the blob came from the box the operator actually set
   up), both keys pass the 32-byte gate, fingerprint is 64 hex chars,
   endpoint parses. On success it stores the relay's identity and
   pinned fingerprint, marks the relay active, and BURNS the token
   (cleared in the same transaction - single use, no replay).

Blobs contain only public halves plus the one-time token, so
re-showing the setup blob while pending is safe. Re-pairing mints a
fresh token and drops the old relay identity - the old box's keys are
dead. Neither side proves anything cryptographically DURING pairing;
the binding is the operator plus the token round-trip. All secrecy
guarantees come from the keys exchanged, enforced on every subsequent
request.

## Transport: pinned self-signed TLS

The relay self-signs a 10-year ECDSA P-256 certificate for its public
host at setup (SAN = hostname or IP; `crypto/x509` only). The node
NEVER does chain or name validation - it pins the leaf:

```go
TLSClientConfig: &tls.Config{
    InsecureSkipVerify: true,      // the pin IS the trust decision
    MinVersion:         tls.VersionTLS12,
    VerifyPeerCertificate: func(rawCerts [][]byte, _ [][]*x509.Certificate) error {
        sum := sha256.Sum256(rawCerts[0])
        if !bytes.Equal(sum[:], pinnedFP) {
            return fmt.Errorf("certificate does not match the pinned fingerprint")
        }
        return nil
    },
}
```

Pinning the exact key from the handshake is stronger than CA
validation and keeps the relay dependency-free (no ACME, no renewals
that matter - expiry is irrelevant to a pin). The node always dials
the relay; the relay stores NO address for the node.

## Request authentication - signed HTTP

Every control-plane request carries one header:

```
X-PCP-Auth: v1;ts=<unix>;nonce=<b64url 16 bytes>;sig=<b64url ed25519 sig>
```

The signed message (both sides build it identically):

```go
func authMessage(method, path, ts, nonce string, body []byte) []byte {
    bh := sha256.Sum256(body)
    return []byte(strings.Join([]string{
        "pcp-wire-auth-v1",      // context string: domain separation
        method, path, ts, nonce,
        hex.EncodeToString(bh[:]),
    }, "\n"))
}
```

`path` is the full request target - path AND query
(`r.URL.RequestURI()` server-side, `req.URL.RequestURI()` client-side
after parsing, so escaping can't drift). The verifier holds the
node's control PUBLIC key from pairing and enforces:

- signature valid (ed25519.Verify);
- timestamp within ±5 minutes of local clock (`MaxSkew`);
- nonce unseen within the window - a map nonce→expiry, entry expires
  at now+MaxSkew, swept every 256 verifications. Because stale
  timestamps are refused outright, the cache only ever needs one
  window of nonces.

Requests failing any check get 401 and a log line; nothing else. The
relay accepts signed requests from exactly one key - there are no
users, roles, or sessions on a relay.

## Payload sealing - encryption above TLS

Payloads whose plaintext must never exist on the relay's disk (or
must survive a TLS compromise) are additionally sealed to the
RECIPIENT's X25519 public key, ECIES-style with a fresh ephemeral key
per message:

```
envelope = "PCPS1" | ephemeral_pub(32) | nonce(12) | ciphertext+tag
```

- ECDH: `shared = X25519(ephemeral_priv, recipient_pub)`
- KDF: HKDF-SHA256, salt = ephemeral_pub‖recipient_pub,
  info = `"pcp-wire-seal-v1"` → 32-byte ChaCha20-Poly1305 key
- AEAD: random 12-byte nonce, AAD = the magic bytes

Binding BOTH public keys into the HKDF salt means an envelope
transplanted to a different recipient (or with a swapped ephemeral)
derives a different key and fails to open. Sealing needs only the
recipient's public key, so a relay can seal inbound data to the node
while being cryptographically unable to read its own spool - the
core theft property. A per-boot seal pair (minted at startup, never
written down) gives you a queue that a crash renders unreadable on
purpose.

## Secrets on disk

- Relay data dir `0700`; every JSON record written `0600`.
  `identity.json` = its own sign/seal private keys + endpoint;
  `peer.json` = the node's PUBLIC keys only; `tls.crt` (0644) /
  `tls.key` (0600).
- Node side: the relay record (its OWN control/seal private halves,
  the relay's public halves, pinned fingerprint, endpoint) lives in
  the node's replicated database.
- Never persist what a fresh push can replace: signing keys the node
  pushes (e.g. DKIM) stay RAM-only on the relay and are re-pushed
  after every relay restart.

## House crypto conventions (reuse these)

**Passwords** - argon2id (`golang.org/x/crypto/argon2`), 64 MiB
memory, 3 passes, 2 lanes; fresh 32-byte salt; 512-bit derived key
(project floor). Self-describing encoded form so params can be raised
without invalidating old hashes; verify recomputes with the STORED
params and compares with `subtle.ConstantTimeCompare`:

```
$argon2id$v=19$m=65536,t=3,p=2$<b64 salt>$<b64 512-bit key>
```

**Tokens** - `RandomToken(n)`: n bytes of `crypto/rand`, base64url
raw. Used for session tokens, pairing tokens, join secrets. A dead
entropy source panics - nothing sensible continues without it.

**TOTP** - RFC 6238 over RFC 4226, stdlib only: HMAC-SHA1, 30-second
step, 6 digits (the parameters every authenticator app actually
honors - URI overrides are widely ignored, so don't use them). New
secrets: 20 random bytes, unpadded base32. Verify with ±1 step
window, constant-time compare per candidate, and RETURN the matched
step so the caller persists it and refuses reuse (each code accepted
at most once). Enrollment via
`otpauth://totp/<issuer>:<account>?secret=…&issuer=…`.

**Certificates** - plain `crypto/x509`, ECDSA P-256 everywhere. An
internal CA self-signs for 10 years; leaf certs live 90 days, carry
both server- and client-auth EKUs (peers dial each other), SANs
always include localhost + 127.0.0.1/::1, and 128-bit random serials.
Fingerprints are sha256 of the DER (colon-separated uppercase hex for
display, bare hex for pins). Cluster join tokens are one base64url
line of JSON: endpoint + CA fingerprint + short-lived secret + PSK -
same pasted-blob philosophy as pairing.

## Rules

- One keypair, one job: sign with ed25519, seal with X25519 - never
  reuse a key across purposes, and domain-separate every signature
  and KDF with a protocol context string.
- The pairing token is single-use and burned transactionally with
  activation. Re-pair = new token + old identity dropped.
- Pin certificate fingerprints, don't validate chains, for peers you
  paired by hand. Expiry is a non-event; rotation = re-pair.
- Sign the full request target including the query string; verify
  against `RequestURI()` - an unsigned query is an unauthenticated
  input.
- Replay defense needs BOTH a skew bound and a nonce cache; the bound
  is what keeps the cache small.
- Whatever theft must not reveal, seal to a key the relay doesn't
  hold - and prove it in a test that greps the data dir for known
  plaintext after a full exercise (spool a secret message, then walk
  every file asserting it never appears).
- All comparisons of secret-derived values are constant-time.
