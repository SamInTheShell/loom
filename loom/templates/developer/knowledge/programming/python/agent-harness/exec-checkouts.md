# Exec checkouts — a filesystem for the shell, a commit for every step

Shell commands need a real filesystem; the depot stores bare repos
(git-depot.md). The checkout layer bridges the two without giving up
the invariant that every change the agent makes becomes a commit:
each chat gets a private checkout, the tree is synced to the branch
tip before every command, and whatever the command changed is
auto-committed and pushed back to the bare repo afterwards. The user
never sees a "dirty working copy" — history in the depot IS the
state. The container that executes the commands is
shell-containers.md; this file is what it mounts and what happens
around every exec.

## The tree / git / home split

```
~/.yourapp/checkouts/<chat-id>/
    tree/   working tree — the ONLY directory mounted (/workspace)
    git/    the real git dir — NEVER mounted
    home/   container $HOME (caches survive between commands)
```

The split is the security boundary: `--separate-git-dir` keeps all
git metadata out of the tree, so the container sees plain files and
git never has to run inside it (shell-containers.md explains what
that buys). `home/` exists so pip/npm/cargo caches, shell history and
dotfiles persist across container recreation without polluting the
tree. Sanitize the chat id (alnum plus `-_`, capped length) before
using it as a path component.

## Creation — shared clone, alternates

```
git clone --shared --separate-git-dir <root>/git \
    -b <branch> <bare-repo-path> <root>/tree
```

`--shared` writes an alternates file pointing at the bare repo's
object store instead of copying objects: creation cost is the
checkout itself, even for large histories, and the checkout stays
small forever. Two follow-ups are mandatory:

1. Delete the `.git` FILE the clone leaves in the tree (with
   `--separate-git-dir` it is a one-line `gitdir:` pointer). The
   container must never see a usable git pointer; every later sync
   deletes it again, because a container command may have planted its
   own `.git` in /workspace.
2. `git config gc.auto 0` in the checkout. A shared clone that
   repacks or prunes on its own can destroy borrowed objects —
   garbage collection is the depot's job, never the checkout's.

If `git/HEAD` is missing, treat the checkout as half-created debris:
`rmtree` the root and clone fresh. If it exists but HEAD's branch
differs from the requested one (the chat switched branches), fetch
and `checkout -B <branch> origin/<branch>` in place — the home dir
and warm build state survive the switch.

Every host git call against the checkout passes explicit
`--git-dir <root>/git --work-tree <root>/tree`. This is what makes a
container-planted `.git` inert: host git simply never looks at the
tree for metadata, and hooks/config under `git/` are host-owned and
unreachable from the container.

## Sync before every command

Other chats, the editor, or the user may have moved the branch since
this chat's last command. Before each exec, make tree == branch tip.
The hot path is two rev-parses: branch tip in the bare repo, HEAD in
the checkout — equal means nothing moved, return immediately. When
they differ:

1. Crash recovery first. If `git status --porcelain` is non-empty,
   a previous run died between exec and checkpoint. Commit the
   leftovers as a "recovered uncommitted changes" checkpoint and
   push, then re-read the tip (the push moved it). If that push
   fails — it raced a concurrent branch move — swallow the error;
   the divergence path below still preserves the commit.
2. Ahead? (`merge-base --is-ancestor <tip> HEAD`) — a previous
   checkpoint committed but failed to push. Just push again; done if
   it lands.
3. Behind or diverged. If HEAD is NOT an ancestor of the tip, local
   commits would be orphaned by a reset — force-push them to a
   rescue ref in the bare repo first:

   ```
   git push --force origin HEAD:refs/<app>/rescue/<chat-id>
   ```

   then `fetch`, `reset --hard origin/<branch>`, `clean -fdq`.
   Nothing committed is ever lost: the race loser lands on the
   rescue ref, visible in the depot, mergeable by hand.

Tell the user when either recovery fired ("recovered uncommitted
changes → <sha>", "diverged commits preserved on refs/…/rescue/…") —
silent history surgery destroys trust.

## Checkpoint after every command

After the exec returns (success, failure, cancelled — all of them),
`git status --porcelain` decides:

- Empty → no commit, report "no file changes".
- Anything listed → `add -A`, commit with a message derived from the
  command (first line, truncated; append "(cancelled)" when it was),
  `push origin <branch>:<branch>`. The push is local-disk to the
  bare repo — effectively instant. Report the short sha and file
  count in the tool result so the model knows a checkpoint exists.

Serialize sync → exec → checkpoint under a per-chat lock; two
concurrent commands interleaving their checkpoints corrupt the
story. If the checkpoint itself fails, do not discard anything —
surface a loud WARNING that the changes remain in the checkout and
will be recovered on the next command (the recovery path above is
exactly what makes that promise true).

## Warm build state via .gitignore

`status --porcelain` respects `.gitignore`, so `node_modules/`,
`.venv/`, `target/`, `__pycache__/` are never committed — and
`clean -fdq` (no `-x`) never deletes them either. Ignored build
state therefore stays warm in the tree across commands, syncs, and
container recreations, while the depot stores only real source
changes. This one property is why the second `npm test` is fast.
The flip side: an ignored file is unprotected — only committed
content survives a checkout wipe.

Cap what the exec feeds back to the model: keep total output to
~200 kB, and when composing the tool detail send head (~3 kB) +
tail (~4.5 kB) with an "N bytes omitted" marker — for builds and
tests the verdict lives in the tail.

## Secrets and env material

When a chat uses an environment with secret files, materialize them
on the host under `XDG_RUNTIME_DIR/<app>/envfiles/<chat-id>` when
that dir exists (tmpfs: RAM-backed, wiped at logout) else a 0700
run dir in `~/.yourapp/`; dir 0700, files 0600, rewritten on every
call so edits propagate, and a content digest folded into the
container config hash so a changed definition recreates the
container. Those files mount read-only into a SEPARATE env-kind
container, never the shell one — full contract in secret-envs.md.

## Directory attachments — the no-commit variant

When the chat is attached to a plain host folder instead of a depot
repo, mount the folder itself read-write at /workspace (no checkout,
no sync, no checkpoints — changes land directly in the folder), give
it a separate home dir, and bind-mount protected subdirectories
(`.git`, `.venv`, …) READ-ONLY on top so container commands cannot
corrupt host-specific state. Say so in the tool result: users must
know which mode has an undo history and which does not.

On chat deletion remove everything: containers (both kinds), the
checkout root, the directory-attachment home, and materialized env
files. Never raise from cleanup.

## Rules

- The git dir is never mounted, and host git always passes explicit
  `--git-dir`/`--work-tree` — both halves are required; either alone
  leaves a hole.
- Delete the tree's `.git` pointer at creation AND on every sync — a
  container command may plant one at any time.
- `gc.auto 0` in every shared clone; the checkout must never repack
  or prune borrowed objects.
- Never reset over unpushed commits: recovery-checkpoint the dirty
  tree, retry ahead-pushes, rescue-ref divergence — in that order —
  before any `reset --hard`.
- `clean -fdq` without `-x`, so ignored build state survives; only
  committed content is durable.
- One lock per chat around sync → exec → checkpoint; checkpoint
  failures warn loudly and rely on next-run recovery, never discard.
- Checkpoint decisions come from `git status --porcelain`, nothing
  else — no mtime scans, no manual file lists.
