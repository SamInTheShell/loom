# Forge features - issues, merges, permissions, auto-GC

The forge layer turns a git server (smart-protocols.md +
embedded-go-git.md) into a GitHub-shaped product: namespaces and
repositories, one role resolver every surface shares, grants and
teams, issues with labels and notifications, merge requests with an
in-process merge, and fully automatic maintenance. It is a domain
package over a transactional KV store - plain records, explicit
index rows maintained in the SAME transaction as their record, and
pure permission-rule helpers whose ENFORCEMENT lives in the app
layer next to the role check (../webapps/mvc.md for that split).
Everything below generalizes a verified implementation.

## The key schema

One prefix family per concept; every index is a denormalized copy
or pointer, never a second source of truth:

```
ns/<name>                       namespace registry (user|org kinds)
orgs/<org>  orgmembers/<org>/<user>  userorgs/<user>/<org>
teams/<org>/<teamID>
grants/<repoID>/<subject>       subject = "u:<user>" | "t:<org>/<teamID>"
usergrants/<user>/<invTs>-<repoID>      reverse: "shared with you"
teamgrants/<org>/<teamID>/<repoID>      reverse: fan-out bookkeeping
repo/<repoID>                   record: owner ns, name, visibility,
                                default branch, forkOf, sizeBytes
reponame/<ns>/<name> → repoID   the path lookup + uniqueness claim
forks/<parentID>/<childID>      reverse: fork-block on delete
refs/<repoID>/<refname>  obj/…  objblob/…   (embedded-go-git.md)
seq/<repoID>                    ONE issue+MR number sequence
issues/<repoID>/<n>             issueidx/<repoID>/<state>/<invTs>-<n>
merges/<targetRepoID>/<n>       mergeidx/<repoID>/<state>/<invTs>-<n>
mrsrc/<sourceRepoID>/<targetRepoID>:<n>   push→MR head refresh
comments/<repoID>/<n>/<ts>-<id>           shared by issues AND MRs
labels/<repoID>/<labelID>
assigned/<user>/<invTs>-<repoID>:<n>      open items, issue|mr kind
sshkeys/<user>/<id>  sshfp/<hexFP>        (smart-protocols.md)
```

Repo IDs are random and stable - rename/transfer moves only the
`reponame` row; object and ref keys never move. `invTs` is an
inverted timestamp token so plain key-order Lists read newest-first.

## Permissions: one ladder, one resolver

```go
type Role int
const (
    RoleNone  Role = iota
    RoleRead       // clone/fetch, browse, open + comment issues/MRs
    RoleWrite      // push, merge MRs, triage issues/labels/assignees
    RoleAdmin      // settings, visibility, grants, delete/transfer
)
```

`RoleFor(ctx, user, repo)` is the ONE function every surface calls -
web handlers, API, both wire transports. It returns the maximum of:

1. public visibility → read, for everyone including anonymous
   (`user == ""`);
2. personal namespace: owner → admin;
3. org namespace: org owner → admin; org member → the org's
   default-repo permission (none|read|write);
4. team grants where the user is currently a member;
5. direct user grants.

Nothing else may inline permission logic. Callers hide no-access
repos as 404, never 403. Two overriding rules: a site-level "public
repos disallowed" switch forces visibility private BEFORE the
resolve (pass a copy with `Visibility = private`), and anonymous
users never mutate anything - read may open issues, but
`user == ""` is refused at the domain with a sign-in error even
though RoleFor granted read.

**Grants**: `grants/<repoID>/<subject>` {role}, with the user-side
reverse row written in the same transaction (team grants fan a
reverse row out to every current member - team sizes are capped, so
the fan-out is bounded and transactional). Removal unwinds the
reverse rows the same way. Reverse rows are hints, not authority:
every dashboard re-gates each row through RoleFor before rendering,
so a stale row can never leak a repo.

**Orgs/teams/members**: membership rows written in both directions
(`orgmembers/`, `userorgs/`) in one transaction; the last-owner
invariant (an org always keeps ≥1 owner) checked INSIDE the
demote/remove transaction; removing a member cascades through every
team seat and the grant reverse rows in the same commit.

## Repositories, forks, deletion

Create claims `reponame/<ns>/<name>` and writes the record in one
transaction (the claim IS the uniqueness check). Fork writes a new
record with `forkOf`, copies the parent's REF rows only (objects
read through the fork chain - embedded-go-git.md), and adds the
`forks/` reverse row; depth-capped. Delete is blocked while (a) any
fork points at the repo or (b) the repo is the SOURCE of an open
merge request into another repo - both re-checked inside the delete
transaction so a racing fork/MR-create conflicts rather than racing
past the guard. Then: record + name row + fork row deleted first
(unreachable-first, so a crashed sweep never leaves a reachable
half-repo), storage families swept after, quota refunded.

## Issues

`seq/<repoID>` is one shared counter for issues AND merge requests,
claimed transactionally (read the counter inside the create
transaction, write n+1 - racing creates conflict and retry,
bounded) so `#N` is unambiguous repo-wide and autolinks always
resolve.

The record carries denormalized state the lists need
(`CommentCount`, and `IdxID` - the current index row's token so a
re-file deletes the old row by key, never by scan). Three index
moves ride every mutation through one helper:

```go
// load record → apply fn → if state or IdxID changed, delete the
// old issueidx row → rewrite record + fresh index copy. ONE tx.
mutateIssue(ctx, repoID, n, func(tx, issue) error { … })
```

- new activity (comment, state change) restamps `UpdatedAt` and
  `IdxID`, re-filing the row to the top of its state's list;
- the `assigned/<user>/` index holds OPEN items only: closing
  removes the assignees' rows, reopening restores them, in the same
  transaction - the dashboard's "assigned to you" is then one
  bounded List (re-gated through RoleFor);
- comments are `<ts>-<rand>`-keyed so key order is thread order;
  add bumps the count and re-files activity in one transaction.

Permission rules are pure helpers the app layer applies: read opens
and comments (public reporting); write triages (labels, assignees,
close anything); authors close/reopen their OWN with only read.
Read-role creators get their submitted assignees/labels cleared -
triage fields are write-role.

**Labels**: `{id, name, #rrggbb}` per repo, bounded set,
name-unique. Deleting a label removes the record ONLY - issues keep
the dangling id and readers filter through the live label map at
render time. Eager removal would rewrite unbounded records; the
lazy filter is one map lookup.

**Notifications** (best-effort, never failing the mutation):
assign → the assignee; open → creation assignees + @-mentions in
the body; comment → author + assignees + prior commenters, with
@-mentioned users getting the mention flavor instead; close/reopen
→ the author. One notification per person, never the actor.
@-mentions resolve against EXISTING accounts only. Email copies
only when mail is enabled AND the recipient opted in.

## Merge requests

An MR is keyed by TARGET repo on the shared sequence:
`{title, body, author, sourceRepoID, sourceBranch, targetBranch,
state open|merged|closed, headSHA, mergeBase, check, mergedCommit,
assignees, labels, …}` plus a mergeidx activity index and assigned
rows exactly like issues (a `kind` field tells the shared index
apart). Comments reuse the issue family verbatim - the shared
sequence keeps the keys collision-free. The `mrsrc/` row (source
branch in the VALUE - branch names contain `/` and would break
key-prefix isolation) lets receive-pack find open MRs by source.

**Head snapshot + refresh**: `HeadSHA` snapshots the source branch
at create. Every push (and web commit) that moves a branch scans
`mrsrc/<sourceRepoID>/` and, for matching open MRs, re-snapshots
the head, drops the cached check, re-files activity, and notifies
the author - best-effort, after the push already succeeded. GC
anchors open MR heads (embedded-go-git.md), so the snapshot's
objects survive even a deleted source branch.

**Mergeability cache**: the check is stored on the record keyed by
the exact `(HeadSHA, TargetSHA)` pair it was computed against - a
moved head on either side makes it stale BY COMPARISON; no explicit
invalidation exists. The page-render check caps the tree diff
(~2000 changed paths; over it: "checked when you merge"); the merge
attempt computes uncapped.

**The merge algorithm** - file-level three-way, pure go-git, no
line-level diff3 (a deliberate v1 cut: conflicts are per-file):

1. Merge base: `ca.MergeBase(cb)` (first base wins). Base == source
   head → nothing to merge. Base == target head → fast-forward: the
   result IS the source head, no new commit.
2. Tree-diff base→source and base→target WITHOUT rename detection
   (pass nil options - a rename is a delete + add at file level)
   into `map[path]{exists, blobHash, mode}` of final states.
3. Per path: changed on one side only → take it; final states EQUAL
   on both sides → skip (covers identical modify/modify, add/add,
   delete/delete in one comparison); states differ → conflict.
   Conflicts return the sorted file list; the UI's remedy is "merge
   the target into your source locally and push".
4. Cross-fork sources: reads go through a COMBINED storer (union of
   target and source fork chains - the source may be an ancestor,
   descendant, or sibling fork); then copy every object reachable
   from the source head but not from the target's refs into the
   target's own store (`revlist.Objects(combined, [head],
   targetRefHashes)`), charged to the target's quota - after the
   merge the target must reach the whole history on its own chain.
5. Non-FF: rebuild the merged tree from the target tree + the
   source-side change set (the tree-rebuild core from
   embedded-go-git.md; refuse an empty result), then a merge commit
   with `ParentHashes: [targetHead, sourceHead]`, committer = the
   merging user.
6. ONE closing transaction: CAS the target ref against the head the
   classification used, flip the MR to merged, write the "merged
   as `abc12345`" activity comment, re-file the index, unwind
   assigned + mrsrc rows, land the size delta. A lost CAS (target
   moved, or the MR itself changed) sweeps the staged objects,
   refunds the charge, and answers "target moved - re-check".

Rules mirrored from issues: read opens MRs (plus read on the
source, checked at create - the source must be the target or a
fork-network repo, and an unreadable source answers "no such
repository", unconfirmable); write on the TARGET merges; authors
close/reopen their own; "merged" is terminal. Reopen re-snapshots
the source head (refusing if the branch is gone) and drops the
cache.

## Automatic GC policy

Users never get a GC button. Two triggers drive the collector from
embedded-go-git.md:

- **Debounced post-push**: every applied ref batch is inspected -
  deletions schedule a GC immediately; moved refs are checked on a
  background goroutine (old tip an ancestor of new? fast-forwards
  never orphan; anything unresolvable counts as orphaning -
  a spurious GC is wasted work, never wrong). Scheduling (re)arms
  one `time.AfterFunc` per repo (~30 s): a burst of force-pushes
  collects once, and the push response never waits. Failures log
  and rely on the next trigger or the sweep - no retry loops.
- **Nightly sweep**: a loop ticking every ~15 min compares a
  PERSISTED last-sweep timestamp against the ~24 h cadence (so the
  rhythm survives restarts and replicas never stampede), takes a
  cluster-wide singleton lock, then walks every repo with a
  TRY-lock collection - a repo mid-push is skipped, not waited on -
  paced ~1 s apart so a big install never sees back-to-back
  reachability walks. Gate each pass on the service's master
  switch; record the pass for an admin workers page.

## Web routes and UI structure

The app layer is thin handlers over the domain (../webapps/mvc.md):
parse → gate → call domain → render a typed page struct into a
per-page template. The route surface that carries all of the above,
under one mount:

```
/git                          dashboard: your repos, shared, assigned
/git/settings                 profile, credential mint, SSH keys
/git/orgs/{org}/…             members, teams, settings (owner-gated)
/git/new  /git/create         repo creation; {ns}/{repo}/fork
/git/{ns}/{repo}              repo home (readme render)
  /tree|blob|raw|history|blame/{rest...}      browse (read)
  /commits/{rest...}  /commit/{sha}           log + diff (read)
  /branches  /tags    (+ POST create/default/delete)
  /edit|new/{rest...}                         web editor (write)
  /issues[/new|/{n}|/{n}/comment|state|labels|assign]
  /merges[/new|/{n}|/{n}/…|/{n}/merge]
  /labels/…   /settings   /grants/…           (write/admin)
  /info/refs  /git-upload-pack  /git-receive-pack   (the wire)
```

Conventions that make it hold together: literal segments
(`issues`, `info/refs`) win over wildcards in Go 1.22's mux, so the
wire and feature routes never collide with `{rest...}` browse
paths; `{rest...}` carries `{ref}/{path...}` resolved by
longest-match (embedded-go-git.md); read pages are session-OPTIONAL
(anonymous reaches public repos only, and every anonymous request
404s while public repos are disallowed), all mutations are
session-gated + CSRF-checked + audited through one shared wrapper;
a feature master switch turns every route into a 404 so disabled is
indistinguishable from unbuilt. Issue and MR views redirect each
other's numbers (shared sequence), so `#N` links always land.

## Rules

- Record and index move in the SAME transaction, always - the
  `IdxID` re-file trick avoids index scans; a crashed half-write
  must be impossible, not rare.
- Indexes and reverse rows are hints; RoleFor at render time is the
  authority. Never trust a stored row to still be visible.
- Claim uniqueness (names, sequence numbers, SSH fingerprints) by
  transactional read-then-write on the index row itself; retry
  bounded on conflict.
- Deletion guards live INSIDE the delete transaction (fork rows,
  mrsrc rows) or they are raceable.
- Every list the UI renders is bounded (page limits, count
  ceilings, scan caps) - forge pages must degrade, never wedge.
- Notifications, cache warm-ups, and head refreshes are
  best-effort AFTER the mutation committed; a lost side-effect
  never fails or reorders the action.
- The merge is only correct because the closing CAS re-verifies the
  exact target head the classification used - keep computation and
  commit honest about the race between them.
