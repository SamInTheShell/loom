# Self-hosting git — the assembly map

This folder is a component catalog for embedding a complete git
hosting system — a small forge — inside a Go application: no cgit, no
gitolite, no shelling out to `git` at all. go-git supplies the object
model and the protocol plumbing; everything else (storage, transports,
web UI, issues, merge requests, permissions, GC) is application code.
Each file covers one part with concrete APIs and contracts; this
README shows how they compose.

## The layer diagram

```
git CLI clients          browsers
      │                     │
smart-protocols.md    forge-features.md (web routes)
  HTTP wire + SSH       browse / edit / issues / MRs
      │                     │
      └───────┬─────────────┘
              │  both gate through ONE role resolver
              │  (forge-features.md: RoleFor)
              │
embedded-go-git.md      the git engine: a storage.Storer
                        implementation + go-git object walks,
                        diffs, blame, programmatic commits, GC
              │
        your storage    KV/blob store, or filesystem bare
                        repos — the storer seam decides
```

## Which file answers what

1. **smart-protocols.md** — serving real `git clone/fetch/push` over
   smart HTTP (`/info/refs?service=…`, pkt-line framing, the
   stateless-rpc v0 exchange, probe flush-pkts, gzip bodies) and over
   SSH (`x/crypto/ssh` server, publickey auth against stored keys,
   exec whitelisting, the interactive negotiation loop). One shared
   protocol core, two thin transports.
2. **embedded-go-git.md** — go-git as the embedded engine: writing a
   `storage.Storer` over your own store, reading refs/commits/trees/
   blobs for web pages, unified diffs, per-path logs, blame, the
   last-commit tree-listing cache, building commits programmatically
   (web editor, init commit, merge commits), and pure-Go GC by
   reachability.
3. **forge-features.md** — the forge model layer: roles and grants,
   orgs/teams/members, issues with labels and notifications, merge
   requests with a file-level three-way merge, automatic GC policy,
   and the web route structure that exposes it all.

## How the pieces compose into a forge

- **One storer, every consumer.** The `storage.Storer` implementation
  in embedded-go-git.md is opened per request and handed to go-git's
  server sessions (protocols), to object walks (browse pages), and to
  the merge/GC machinery (forge features). Nothing touches objects or
  refs except through it.
- **One permission function.** Every surface — HTTP wire, SSH,
  browse pages, issue mutations — resolves access through the single
  `RoleFor` (forge-features.md). Transports authenticate differently
  (API-key Basic auth, SSH publickey, browser session) but authorize
  identically, and no-access always answers "not found", never 403.
- **One push path.** HTTP receive-pack, SSH receive-pack, the web
  editor's commit, and the merge button all end at the same atomic
  multi-ref CAS transaction, the same quota charge, the same per-repo
  push lock, and the same post-update hooks (MR head refresh,
  debounced GC).

## Configurations you can assemble

1. **Read-only mirror** — the storer + `Advertise`/upload-pack from
   smart-protocols.md. Clients can clone; nothing writes.
2. **Push server** — add receive-pack: the push lock, quota, atomic
   ref updates. Still no UI.
3. **Code browser** — add the read surfaces of embedded-go-git.md
   (trees, blobs, logs, diffs, blame) behind web routes.
4. **Full forge** — add forge-features.md: issues, merge requests,
   grants, auto-GC. Each layer reuses the previous ones unchanged.

## Reading order for building

1. embedded-go-git.md — the storer is the foundation; nothing works
   without it.
2. smart-protocols.md — clone/push proves the storer against stock
   git, the best conformance test you will ever get for free.
3. forge-features.md — the model layer and UI on top.

For the app/domain split these files assume (thin HTTP handlers over
a domain package that owns all state), see ../webapps/mvc.md. Test
patterns are in ../testing.md — drive the wire endpoints with a real
`git` binary in a temp dir; nothing else exercises the protocol
honestly.
