# Embedded go-git — the git engine inside your process

go-git is a pure-Go git implementation whose object model (commits,
trees, blobs, tags), pack machinery, and protocol sessions all run
against one seam: `storage.Storer`. Implement that interface over
YOUR storage and you get clone/push serving, web browsing, diffs,
blame, programmatic commits, and GC — with no `git` binary and no
filesystem layout requirements. This file is the engine;
smart-protocols.md serves it on the wire and forge-features.md
builds the forge on top.

```
go get github.com/go-git/go-git/v5
```

## Storage: implement storage.Storer yourself

Two honest options. For a filesystem forge, bare repos via
`filesystem.NewStorage(osfs.New(path), cache.NewObjectLRUDefault())`
work out of the box (go-billy supplies the fs abstraction). But a
custom storer over a transactional KV/blob store buys atomic
multi-ref updates, replication, and quota accounting for free — and
is less code than it sounds, because the wire protocol and object
walks only exercise a small subset. The KV design, verified in
production shape:

**Refs** — one KV row per ref: `refs/<repoID>/<refname>` → 40-hex
sha. The raw refname is the key suffix, so validate refnames to
printable ASCII + git's own rules (`plumbing.ReferenceName.
Validate()`) before they become keys. HEAD is never stored: it is
derived as a symbolic ref to the repo record's default branch:

```go
func (r *RepoStorer) Reference(name plumbing.ReferenceName) (*plumbing.Reference, error) {
    if name == plumbing.HEAD {
        return plumbing.NewSymbolicReference(plumbing.HEAD,
            plumbing.NewBranchReferenceName(r.repo.DefaultBranch)), nil
    }
    // KV get on refKey(repoID, name); miss → plumbing.ErrReferenceNotFound
}
```

`IterReferences` returns stored refs plus the synthetic HEAD.
`PackRefs` is a no-op (refs are already rows). Renaming the default
branch is a one-field record update — HEAD follows.

**Objects** — loose only, no packfiles at rest. Encode each object
as a one-line header + compressed body:

```go
func encodeObject(t plumbing.ObjectType, content []byte) []byte {
    var buf bytes.Buffer
    fmt.Fprintf(&buf, "%s %d\n", t.String(), len(content))
    zw := zlib.NewWriter(&buf)
    zw.Write(content); zw.Close()
    return buf.Bytes()
}
```

The header makes type/size stat (`EncodedObjectSize`) readable
without inflating — store small encodings (< ~256 KiB) as KV values
under `obj/<repoID>/<sha>` and large ones in a blob tier under
`objblob/<repoID>/<sha>` where a 64-byte ranged read serves the
header. `SetEncodedObject` computes the hash itself
(`plumbing.ComputeHash(type, content)`), dedups via
`HasEncodedObject` (an object already present stores nothing — this
alone makes unchanged files free), and enforces two write-side caps:
per-object bytes and per-push object count.

**Write buffering** — pack unpacking calls `SetEncodedObject` tens
of thousands of times. Stage small objects in memory and flush in
chunked transactions (e.g. ≤200 keys / ≤2 MiB per commit, pure Sets
so flushes never conflict); remember every written key so `Abort()`
can sweep a failed push clean. Keep a bounded read/write-back cache
of encoded objects — packfile delta resolution re-reads bases
constantly and will hammer your store without it. Expose
`StoredBytes()` (what this storer wrote) and an optional
`OnStored(delta)` hook — that pair is the quota integration
(smart-protocols.md).

**Honest stubs** — the wire protocol and object walks never touch
the rest: `Shallow` → nil, `SetShallow`/`SetIndex`/`SetConfig` →
"unsupported" error, `Index()` → empty v2 index, `Config()` → fresh
`config.NewConfig()`, `Module()` → `memory.NewStorage()`. Declare
`var _ storage.Storer = (*RepoStorer)(nil)` and let the compiler
keep you complete.

**Fork sharing (optional)** — give the storer a read chain: lookups
miss through the repo's fork-parent IDs (depth-capped), writes
always land in the leaf. Forking then copies only the parent's ref
rows — O(refs), zero object bytes — and the dedup check against the
chain means forks are charged only for what they add.

The storer is request-scoped: it carries the request context
(go-git's interfaces take none) and is not reused across requests.

## Atomic ref updates

Pushes and every programmatic commit go through one function, not
`SetReference`: apply the whole batch of `{Name, Old, New}` commands
in a single storage transaction, compare-and-swapping each stored
value against `Old` (zero = must-not-exist; `New` zero = delete). A
stale value fails the entire batch — git's atomic multi-ref push
semantics. Update the repo's size accounting in the same
transaction, and fire post-update hooks (GC scheduling, MR head
refresh — forge-features.md) only after commit.

## Reading for the web: refs, trees, blobs, logs

Everything below is `plumbing/object` over the storer; nothing
mutates.

```go
c, err := object.GetCommit(sto, hash)        // commit header + tree
tree, _ := c.Tree()
sub, _  := tree.Tree("src/pkg")              // subdirectory
entry, _ := tree.FindEntry("src/main.go")    // (name, mode, hash)
blob, _ := object.GetBlob(sto, entry.Hash)   // rd, _ := blob.Reader()
tag, err := object.GetTag(sto, h)            // annotated tag (peel via tag.Commit())
```

- **Ref resolution** for a user-supplied string: try branch name,
  then tag name (peeling annotated tags to their commit), then — if
  exactly 40 hex — a commit lookup. Not found is the page's 404.
- **URLs** shaped `/{ref}/{path...}` are ambiguous because branch
  names contain `/`. Split by longest match: iterate branch+tag
  names, take the longest one that prefixes the rest; otherwise cut
  at the first slash (the sha case).
- **Directory listings**: `tree.Entries` with sizes from
  `EncodedObjectSize` (the header stat — never inflate blobs to
  list a directory). Dirs first, then name order.
- **File views**: cap rendered content (~1 MiB); over the cap read
  only the first 8000 bytes for the binary sniff (a NUL byte —
  git's own heuristic) and mark it too-large so the page offers the
  raw download. Missing objects and refs must degrade pages, not
  500 them.
- **Commit log**: `object.NewCommitPreorderIter(start, nil, nil)`,
  take `limit`, and return the NEXT commit's hash as the cursor —
  a hash is a perfect stateless pagination token.

## Diffs

Tree-diff two commits and render each change with the unified
encoder. An initial commit (or unrelated histories) diffs against
`&object.Tree{}` — the empty tree.

```go
changes, _ := object.DiffTreeWithOptions(ctx, fromTree, toTree,
    object.DefaultDiffTreeOptions)         // rename detection on
for _, chg := range changes {
    patch, err := chg.PatchContext(ctx)
    fp := patch.FilePatches()[0]
    if fp.IsBinary() { /* binary marker, no lines */ }
    var buf bytes.Buffer
    fdiff.NewUnifiedEncoder(&buf, fdiff.DefaultContextLines).Encode(patch)
    // rendered rows start at the first "@@" hunk header; parse
    // +/-/space/"\ No newline" prefixes into typed line rows
}
```

Caps, enforced honestly: per-file (check BLOB sizes via the header
stat before loading — a huge file never gets diffed) and total
rendered bytes across the diff (over it, remaining files list by
name only, lines dropped, with a truncation count the page states).
A single-commit page diffs against the FIRST parent; a merge-request
diff calls the same function with the merge base as `from`
(forge-features.md).

## Per-path history and blame

**File log** — walk first-parents from the page's head and keep
commits where the tree entry at the EXACT path differs from the
first parent: compare `entry.Hash.String() + ":" +
entry.Mode.String()` (mode is identity too — chmod counts), with
absence on either side counting as a change. No rename following —
a rename is honestly a delete + an add. Cap the SCAN per page
(~1000 commits stepped) independently of the match limit and return
a `capped` flag so a huge history renders "search stopped early,
continue" instead of wedging the request.

**Blame** — go-git has it natively:

```go
c, _ := object.GetCommit(sto, commit)
res, err := gogit.Blame(c, path)     // res.Lines: Hash, AuthorName, Date, Text
```

It is expensive on deep churny histories: refuse binary/oversized
files and files over a few thousand lines up front, and run the call
in a goroutine raced against a ~10 s timer — on timeout render an
honest refusal that links the file-history page instead. The
storer's request context bounds the abandoned computation's reads.

## The last-commit cache

Tree listings that show "last commit touching this entry" per row
must NOT run one file-log per entry (O(files × history)). Do ONE
bounded first-parent walk per (commit, directory): resolve the
listed directory's tree in the current commit and its first parent;
if the two tree hashes are equal, nothing here changed — step. Else
attribute every still-pending entry whose (hash, mode) differs from
— or is absent in — the parent to the current commit, and continue
until all entries are attributed or a step cap (~400) trips.
Unattributed entries render blank: honest, never slow.

Cache the resulting map keyed by (repoID, commit sha, hash-of-dir):
a commit's history never changes, so entries are IMMUTABLE — a
rebuildable cache with no invalidation story at all. Skip caching
oversized results; hash the directory path into the key (paths
carry slashes and arbitrary bytes).

## Creating commits programmatically

The web editor, the "initialize with README" commit, and merge
commits all build real objects through the storer:

```go
// blob
obj := sto.NewEncodedObject()
obj.SetType(plumbing.BlobObject)
w, _ := obj.Writer(); w.Write(content); w.Close()
blobHash, _ := sto.SetEncodedObject(obj)

// tree (one level; nest by storing subtree hashes as Dir entries)
tree := object.Tree{Entries: []object.TreeEntry{
    {Name: "README.md", Mode: filemode.Regular, Hash: blobHash},
}}
tobj := sto.NewEncodedObject(); tree.Encode(tobj)
treeHash, _ := sto.SetEncodedObject(tobj)

// commit
sig := object.Signature{Name: name, Email: email, When: time.Now()}
commit := object.Commit{Author: sig, Committer: sig,
    Message: "Initial commit\n", TreeHash: treeHash,
    ParentHashes: []plumbing.Hash{parent}}      // omit for root
cobj := sto.NewEncodedObject(); commit.Encode(cobj)
commitHash, _ := sto.SetEncodedObject(cobj)
// then Flush + the atomic ref CAS {branch, Old: parent, New: commitHash}
```

**Tree rebuilds** (edit/delete/rename existing files): materialize
the parent tree's leaves into `map[path]{hash, mode}` with
`object.NewTreeWalker(tree, true, nil)` (skip Dir entries), apply
the change set (delete = remove key), then write tree objects
bottom-up per directory level. Order entries by git's tree rule:
byte-wise on the name with directories comparing as `name + "/"` —
get this wrong and the hashes won't match stock git's.

**The editor's CAS story**: the page captures the branch head
(BaseSHA). On save, if the branch moved, compare the touched path's
(hash, mode) state between BaseSHA and the current head — unchanged
there means the commit rebases transparently onto the new head;
changed means an edit-conflict error that re-renders with the
user's content intact. If the tree hash equals the parent's, every
object deduplicated: abort and report "nothing to commit". A ref
CAS lost to a concurrent push re-resolves and retries (bounded, ~3).
Serialize the whole save under the repo push lock — a web commit is
a push in every way that matters. Validate user paths before they
become tree entries: slash-separated, no empty/`.`/`..`/`.git`
segments, no control bytes, bounded length.

## GC — pure-Go reachability

Deleted branches and force-pushes orphan objects; nothing on disk
removes them. Collect per repo, in-process:

```go
hashes, err := revlist.Objects(sto, starts, ignore)
// every object reachable from `starts` but not from `ignore`
```

1. **Anchors** (`starts`): every ref of the repo, every ref of every
   fork DESCENDANT (they read through this store), and the head
   snapshot of every open merge request sourced from this repo
   (its diff still reads those objects after the branch moves).
   Drop anchors whose object is already gone — revlist errors on
   unknown hashes.
2. Enumerate the repo's OWN object keys (both tiers); delete every
   sha not in the reachable set, in chunked transactions; refund
   the freed bytes to the owner's quota and the repo size record.
3. Hold the same per-repo push lock receive-pack holds (an
   in-process mutex per repoID plus a cross-replica store lock) —
   an in-flight push's staged-but-unreferenced objects must never
   be swept.

There is no shelling out to `git gc` and no repacking — loose-only
storage trades pack-level compression for exactly this simplicity.
WHEN gc runs (debounce after orphaning pushes + a nightly sweep) is
policy, covered in forge-features.md.

## Rules

- Nothing writes objects or refs except through the storer;
  multi-ref updates only through the atomic CAS batch. Two write
  paths = corruption you'll find months later.
- Stat, don't read: directory sizes, diff pre-checks, and existence
  checks use the encoded header, never blob inflation.
- Every walk is bounded and every bound is surfaced (capped flags,
  truncation counts, blame refusals). A forge's worst enemy is one
  pathological repo wedging request handlers.
- Root commits, empty trees, annotated tags, and mode-only changes
  are the edge cases in EVERY walk above — test each explicitly.
- Verify programmatic commits against stock git: clone the repo and
  `git fsck`; tree-sort or header bugs surface immediately
  (../testing.md).
- Request-scope the storer; its context, caches, and staged writes
  must die with the request.
