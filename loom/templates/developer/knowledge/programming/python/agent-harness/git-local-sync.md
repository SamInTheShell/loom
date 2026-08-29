# Local sync — linking depot repos to folders on the user's disk

Users have normal git working copies; the app has bare depot repos
(git-depot.md). Local sync bridges them WITHOUT inventing a mirror
format: a linked folder is treated as a REMOTE whose URL is a
filesystem path, and sync is fetch/push with fast-forward-only
pointer moves in both directions. Three principles are load-bearing:
nothing runs automatically and nothing is reachable by agents — every
function sits behind an explicit user click, so any change in the
user's folder traces to a button; branch pointers only ever move
fast-forward, and the folder's checked-out branch is only touched when
its worktree is clean; divergence is never auto-merged — it is
materialized as an ordinary depot branch carrying conflict markers,
resolved with ordinary tools, and completed as a real two-parent
merge.

## Registry and scopes

Store links in the bare repo's own git config — it travels with the
repo and needs no extra database:

```
app-folder.<id>.path     absolute path of the linked folder
app-folder.<id>.branch   scope; empty/absent = all branches
```

`<id>` is a short random token (`"f" + uuid4().hex[:8]`). Read links
back with `config --get-regexp '^app-folder\.'`; remove with
`config --remove-section app-folder.<id>`. A repo may have several
linked folders, each with a SCOPE: all branches (the classic "my
working copy" mirror) or exactly one branch (a clone made to test a
specific branch — sync then never looks at any other branch in that
folder). Refuse to link the same path twice. Linking requires an
existing git repo at the path (plain directories go through import —
git-depot.md); as a courtesy, add a remote in the user's repo pointing
at the depot path so plain `git pull <appremote> <branch>` works, and
remove it on unlink.

To materialize a fresh folder instead of linking an existing one,
`clone` the depot to the destination — a FULL clone, not `--shared`:
the folder must survive if the app is uninstalled (shared/alternates
clones are for app-owned checkouts only, exec-checkouts.md). Use
`--branch <b> --single-branch` for a branch-scoped clone, rename
`origin` to your app's remote name, copy the depot's real remotes
alongside, then register the link.

## Snapshots and status

Never read the user's repo ad hoc during comparison. Snapshot its
branch heads into a private ref namespace in the DEPOT:

```
git fetch --prune <folderpath> "+refs/heads/*:refs/app/local/<id>/*"
```

run in the bare repo. This reads the folder and writes only the depot;
`--prune` drops snapshot refs for branches deleted in the folder.
Every status/sync starts with this fetch, so all comparisons are
between two sets of depot-side refs, enumerated with
`for-each-ref <prefix> --format='%(refname) %(objectname)'`.

Classify each branch name present on either side:

- both sides, equal shas → `in-sync`.
- both sides, different → `rev-list --left-right --count d...l` for
  the ahead/behind counts, then `merge-base --is-ancestor d l` /
  `merge-base --is-ancestor l d` (exit codes) decide `local-ahead`,
  `depot-ahead`, or `diverged` (neither is ancestor).
- depot only → `depot-only`; snapshot only → `local-only`.

Also record, from the folder itself: whether it is bare
(`rev-parse --is-bare-repository`), which branch is checked out
(`symbolic-ref --short HEAD`), and the dirty file list
(`status --porcelain`, capped at ~50 entries for display). A folder
linked from an unrelated repo makes every shared branch `diverged` —
detect the all-diverged case at link time and warn up front.

## Sync — one click, ff-only, both directions

Sync walks the classified branches (optionally a single branch, for a
per-branch button; error if it's outside a scoped folder's scope) and
takes exactly one action per branch, collecting a result line for
each so the user sees precisely what moved:

- `in-sync` — nothing.
- `local-only` — adopt: `update-ref refs/heads/<b> <local-sha>` in
  the depot. The folder invented a branch; the depot takes it.
- `local-ahead` — depot fast-forwards:
  `update-ref refs/heads/<b> <local> <depot>` — the third argument
  makes it compare-and-swap, so an agent commit racing the sync
  errors instead of being clobbered.
- `depot-only` / `depot-ahead`, branch NOT checked out in the folder —
  push from the depot: `push <folderpath>
  refs/heads/<b>:refs/heads/<b>`. Git refuses to update the
  checked-out branch of a non-bare repo via push, and refuses
  non-fast-forwards without force — both refusals are your invariants
  enforced for free, so never pass `--force`.
- `depot-ahead`, branch IS checked out — only if
  `status --porcelain` is empty: run IN the folder
  `fetch <depotpath> refs/heads/<b>` then
  `merge --ff-only FETCH_HEAD`, which moves branch, index, and
  worktree together. If the folder is dirty, skip with a message
  ("commit or stash there first") — a dirty worktree is never
  touched, not even for a fast-forward.
- `diverged` — report only ("both sides added commits — use Resolve").
  Never merge here.

Skip any branch with a resolution in flight (below), and skip the
resolution branches themselves. Offer a separate user-driven "commit
my folder" action (`add -A` + `commit -m` run in the folder, retrying
with `-c user.name=... -c user.email=...` fallbacks if the user has
no git identity) so "sync my uncommitted work" is two clicks, both
explicit.

## Divergence — materialize a conflict branch

When both sides advanced, produce a NORMAL depot branch whose tip
commit is the merged tree WITH conflict markers, parented on the depot
side. The user — or an agent they explicitly invite, with ordinary
permission gating — edits it like any branch; the target branch and
the folder stay untouched until completion. The recipe (all in a
throwaway `clone --shared` of the depot; tmpdir removed in `finally`):

1. Resolve `ours` (depot head) and `theirs` (snapshot head); require
   true divergence — anything else is plain Sync's job. Pick an
   unused name like `app/merge/<branch>` (suffix `-2`, `-3`… if
   taken).
2. In the clone: `checkout -b <mergebranch>`, fetch theirs
   (`fetch origin '+refs/app/local/<id>/<b>:refs/tmp/theirs'`), then
   `merge --no-commit --no-ff refs/tmp/theirs`. Collect conflicts
   with `diff --name-only --diff-filter=U`. A failed merge with no
   conflicted files is a real error — surface it.
3. `add -A`, then DELETE `.git/MERGE_HEAD` and `.git/MERGE_MSG`
   before committing. This is the trick: with MERGE_HEAD present, the
   commit would conclude the merge as a two-parent commit and the
   markers would masquerade as resolved. Deleting it demotes the
   result to a single-parent commit that plainly says "merge in
   progress".
4. `commit --allow-empty -m` a message recording ours/theirs shas and
   the conflicted file list, then `push origin <mb>:<mb>`.
5. Record the merge in depot config — `app.merge/<target>.branch`,
   `.ours`, `.theirs`, `.folder` (or `.base` for the branch↔branch
   variant), and one `--add ... .file` entry per conflicted path.

Status of an in-flight merge: `git grep -l -E
'^(<{7}|={7}|>{7})( |$)' <tipsha> -- <files...>` in the bare repo
lists files still carrying markers; also check whether the target
branch moved since `ours` was recorded — if it did, completion must be
refused ("abandon and start again"), because the resolution was built
against a stale parent.

Complete: all markers gone and target unmoved →
`commit-tree <mb-tree> -p <ours> -p <theirs> -m <msg>` builds the
REAL two-parent merge (take the tree via
`rev-parse refs/heads/<mb>^{tree}`), then
`update-ref refs/heads/<target> <m> <ours>` fast-forwards the target
onto it (CAS again). By construction both sides are now ancestors, so
the folder fast-forwards cleanly on the user's NEXT manual sync — the
folder still changes only on a click. Cleanup: delete the resolution
branch, remove the config section. Abandon is just the cleanup.

The same machinery does branch↔branch catch-up with no folder (an
agent's work branch absorbing main): up-to-date / CAS fast-forward /
clean merge in a shared clone (where the push back is the race guard —
a non-fast-forward rejection means the branch moved mid-merge) /
conflicts → the same resolution-branch lifecycle. Always merge, never
rebase: those commits are the user's rollback points, and history is
never rewritten.

## Rules

- Every function here is user-click-only. None of it is exposed to
  agents as tools (tool-catalog.md) and none of it runs on timers or
  file watchers. "Why did my folder change?" must always have the
  answer "you pressed Sync".
- Branch pointers move fast-forward only, both directions; the only
  non-ff writes are `update-ref` CAS moves in the depot, never
  `push --force` at the folder.
- The checked-out branch is special twice: push can't update it
  (route through in-folder `fetch` + `merge --ff-only`), and a dirty
  worktree blocks even that. Never stash, never checkout, never touch
  uncommitted files.
- All comparisons run against snapshot refs in the depot
  (`refs/app/local/<id>/*`), refreshed by `fetch --prune` at the
  start of every status/sync — never against live folder state.
- Divergence produces a conflict branch, never an auto-merge and
  never a rebase. Strip MERGE_HEAD before the in-progress commit;
  build the final merge with explicit `commit-tree -p ours -p theirs`.
- Merge bookkeeping (config entries, snapshot refs) is cleaned on
  complete, abandon, and unlink — stale entries block future syncs of
  that branch by design, so cleanup is correctness, not hygiene.
- Folders created by the app are full clones; `--shared` clones are
  reserved for app-internal throwaway merges and exec checkouts
  (exec-checkouts.md). Network remotes copied into a folder still
  authenticate through the askpass broker (git-depot.md,
  http-over-ssh.md).
