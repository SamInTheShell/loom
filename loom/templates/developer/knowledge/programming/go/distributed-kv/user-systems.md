# User systems — accounts, tokens, authorization

The account and permission layer for a databox-style store. It is
deliberately boring: a small amount of strongly-consistent state (in
the metadata group — metadata-group.md), standard crypto primitives,
and one middleware seam every frontend shares (frontends.md,
wire-protocols.md).

## Data model (lives in the metadata group)

```
user/<name>            User{name, credHash, created, disabled}
grant/<user>/<res>     Grant{perms bitmask}        res = bucket/prefix
token/<tokenID>        Token{userRef, scopeGrants, expiry, secretHash}
```

Users and grants change rarely → metadata-group write rates are fine,
and every node can answer authz from its replicated copy — auth never
adds a network hop to the data path.

## Passwords

`golang.org/x/crypto/argon2` (argon2id), per-user random salt:

```go
salt := make([]byte, 16)
rand.Read(salt)                              // crypto/rand
hash := argon2.IDKey(password, salt, 1, 64*1024, 4, 32)
// store: argon2id$v=19$m=65536,t=1,p=4$<b64 salt>$<b64 hash>
```

Verify by recomputing with the stored parameters and comparing with
`subtle.ConstantTimeCompare`. Store the parameter string alongside so
parameters can be raised later (rehash-on-successful-login when the
stored params are below current policy). Never any other hash — not
bcrypt-because-familiar, definitely not SHA-anything.

## API tokens (the thing clients actually send)

Opaque random tokens, hashed at rest — if the metadata store leaks,
tokens don't:

```go
raw := make([]byte, 32)
rand.Read(raw)
tokenID := base64.RawURLEncoding.EncodeToString(raw[:8])   // lookup key
secret  := base64.RawURLEncoding.EncodeToString(raw[8:])
shown   := "dbx_" + tokenID + "_" + secret                  // shown ONCE
// stored: token/<tokenID> with sha256(secret), scoped grants, expiry
```

Auth check: split the presented token, look up by tokenID, hash the
secret, constant-time compare, check expiry + user not disabled. No
JWTs: self-contained tokens can't be revoked and their alg-confusion
history is a tax; with replicated state on every node the lookup is
local and revocation is a delete.

## Authorization model

Keep it to resources and verbs; RBAC ceremony can come later:

```go
type Perm uint8
const (
    PermRead Perm = 1 << iota   // Get/Scan/blob read
    PermWrite                    // Put/Delete/blob write
    PermAdmin                    // grants, users, cluster ops
)
// resource = bucket or key prefix; grants on a prefix cover its subtree
func Allowed(u *AuthCtx, res string, need Perm) bool
```

Rules:

- Check happens in ONE place: a middleware/interceptor that resolves
  the token to an `AuthCtx` and the handler declares `need` — never
  ad-hoc checks scattered through handlers.
- Deny by default; the error names the missing perm (`need write on
  b/logs`) — debuggable denials get fixed instead of worked around.
- Admin API (frontends.md) requires PermAdmin AND is a separate
  listener/port so a network policy can fence it.
- The property test from build-plan.md M9: for every API operation,
  fuzz (user grants × operation) and assert success ⇔ the required
  perm is present. This test is cheap and catches every future
  forgot-the-check handler.

## Bootstrap and operations

- First start with zero users: create `root` with a one-time random
  password printed to the console/log ONCE (or accept an initial
  password via env/flag). Force a change on first login in the UI.
- Disabled users keep their rows (audit trail); tokens of a disabled
  user fail auth immediately (the check reads the user row too).
- Rate-limit login attempts per user+IP (a token bucket in memory per
  node is fine — it's a nuisance control, not a consistency problem).
- Log auth DECISIONS (who, what, allowed/denied, from where) —
  metadata writes are the natural audit log for grant changes; denials
  go to the normal log.

## Multi-tenancy note

If buckets are tenant boundaries, encode the tenant into the KV key
prefix (`d/<group>/<bucket>/<key>` at the engine level — pebble.md key
schema) so a grant on a bucket is literally a grant on a key range;
range-sharded groups then keep tenants contiguous, which also makes
per-tenant usage accounting an engine range-size query.
