# Git depot - bare repositories as the app's storage core

The workbench stores every project as a BARE git repository under the
app data dir (`~/.yourapp/repos/<id>.git`). Bare repos give you
versioned, branchable, diffable storage with zero invented formats:
the UI reads files with plumbing commands against refs, agents commit
through the same path, and any external git tool interoperates for
free. The catch is that bare repos have no worktree - every read and
every commit must go through plumbing, and network operations must
never be allowed to prompt on a TTY the app doesn't have. This file is
the complete recipe; git-local-sync.md covers mirroring depot repos to
normal folders on disk, exec-checkouts.md covers materializing a
worktree for shell execution, and tool-catalog.md maps these
operations onto agent tools.

## Layout

```
~/.yourapp/
  repos/<id>.git      the depot: one bare repo per project
  bin/askpass-helper  injected into git/ssh for secret prompts
  run/                unix sockets, temp indexes, transient state
  config.json         app config (0600 - may hold API keys)
```

Make the data root overridable by one env var (`YOURAPP_HOME`) so
tests point it at a temp dir. Repo IDs are directory names - validate
with `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$`, reject `..` and a trailing
`.git`, and raise before ever joining the path. Keep the user-facing
display name separate from the id (a small text file inside the bare
dir works); derive the id as a slug of the name and suffix `-2`,
`-3`… while taken, so duplicate display names never fail creation.

## The subprocess wrapper

Drive the `git` CLI via subprocess, not pygit2/dulwich: identical
behavior to the user's own git (credentials, transports, config), one
fewer native dependency, and depot operations are nowhere near
performance-critical at CLI-call granularity. Wrap it once:

```python
@dataclass
class GitResult:
    code: int
    out: str          # stdout decoded utf-8/replace
    err: str
    out_bytes: bytes  # raw stdout - needed for blobs and patches

def run(args, *, cwd=None, env=None, input_bytes=None, timeout=60):
    p = subprocess.run(["git", *args], cwd=cwd, env=env or base_env(),
                       input=input_bytes, capture_output=True,
                       timeout=timeout)
    return GitResult(p.returncode, p.stdout.decode("utf-8", "replace"),
                     p.stderr.decode("utf-8", "replace"), p.stdout)

def must(args, **kw):     # raise GitError on nonzero, keep the result
    ...
```

`base_env()` copies `os.environ` and sets `GIT_TERMINAL_PROMPT=0` -
git must fail, never hang, if anything tries to prompt outside the
askpass path below. Every call gets a timeout (60 s default; 300-900 s
for clone/fetch/push/checkout-scale work). Keep `out_bytes`: file
reads and patches are bytes first, text second.

Commit identity goes through env vars, not repo config:
`GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL`, resolved as
app-config author → `git config user.name/email` → app fallback. The
author/committer split is useful as designed: when the user opts in,
set the AUTHOR name to the model that made the change while the
COMMITTER stays the configured user. Carry the current agent name in a
`contextvars.ContextVar` set around each tool call, so every commit in
that window inherits attribution without threading a parameter through
every layer.

## Reading against refs - no worktree needed

All reads take `ref:path` specs. Sanitize the relative path first:
strip slashes, reject any `..` segment and any leading `-` (argument
injection), and pass `--` before free-form revs where git accepts it.

- Directory listing: `ls-tree -l <ref>:<dir>` - each line is
  `<mode> <type> <sha> <size>\t<name>`; `size` is `-` for trees, mode
  `120000` marks symlinks. List lazily per directory, not the whole
  tree.
- File content: `cat-file blob <ref>:<path>` - bytes. Sniff binary by
  NUL in the first ~8 KB; cap text returned to the UI (e.g. 2 MB) and
  report `truncated`.
- Resolve a ref: `rev-parse <ref>`; tree of a commit:
  `rev-parse <ref>^{tree}`; existence: `show-ref --verify --quiet
  refs/heads/<b>` (exit code only).
- Branch list: `for-each-ref refs/heads` with a `--format` of
  `%(refname:short)%09%(objectname:short)%09%(committerdate:unix)`
  `%09%(authorname)` - one machine-parseable line per branch,
  tab-separated.
- Ahead/behind: `rev-list --left-right --count A...B` (three dots)
  prints two numbers. Use it branch-vs-default and branch-vs-tracking.
- History: `log --format='%H%x09%h%x09%an%x09%ct%x09%P%x09%D%x09%s'
  --max-count=N --skip=M <ref> --` - `%x09` embeds literal tabs so
  splitting is trivial; `%P` gives parents for graph drawing.
- One commit: `show -s --format=...` for metadata, `show --format=
  --stat=110 <sha>` for the stat block, `show --format= --patch
  <sha>` for the diff (cap patch bytes, e.g. 400 KB). Validate the sha
  against `^[0-9a-fA-F]{4,40}$` before interpolating.
- Arbitrary diff: `diff <refA> <refB> -- [path]` works fine in a bare
  repo; both sides are refs.
- Default branch of a bare repo: `symbolic-ref --short HEAD`; set it
  with `symbolic-ref HEAD refs/heads/<b>`.

## Worktree-less commits - the temp-index recipe

A commit is just objects: blobs → tree → commit → ref move. Any
number of file writes and deletions become ONE commit with no
checkout, using a throwaway index file:

```python
def commit_files(repo, branch, files, message):
    # files: [{path, content}]; content None deletes the path
    ref = f"refs/heads/{branch}"
    exists = run(["show-ref", "--verify", "--quiet", ref], cwd=repo).ok
    with tempfile.TemporaryDirectory() as td:
        env = author_env()
        env["GIT_INDEX_FILE"] = os.path.join(td, "index")
        old = None
        if exists:
            old = must(["rev-parse", ref], cwd=repo).out.strip()
            must(["read-tree", ref], cwd=repo, env=env)
        else:
            must(["read-tree", "--empty"], cwd=repo, env=env)
        for f in files:
            if f["content"] is None:      # deletion: zero-sha info line
                must(["update-index", "--index-info"], cwd=repo, env=env,
                     input_bytes=f"0 {'0'*40}\t{f['path']}\n".encode())
            else:
                blob = must(["hash-object", "-w", "--stdin"], cwd=repo,
                            input_bytes=f["content"].encode()).out.strip()
                must(["update-index", "--add", "--cacheinfo",
                      f"100644,{blob},{f['path']}"], cwd=repo, env=env)
        tree = must(["write-tree"], cwd=repo, env=env).out.strip()
        if exists and tree == must(["rev-parse", ref + "^{tree}"],
                                   cwd=repo).out.strip():
            raise GitError("no changes to commit")
        args = ["commit-tree", tree, "-m", message]
        if old:
            args += ["-p", old]
        sha = must(args, cwd=repo, env=env).out.strip()
        # compare-and-swap: fails if the branch moved under us
        must(["update-ref", ref, sha] + ([old] if old else []), cwd=repo)
```

The details that matter: `GIT_INDEX_FILE` is what makes the index
private - never touch the repo's real index. Deletions must go through
`update-index --index-info` with the all-zeros sha (`rm --cached`
paths misbehave in bare repos). Compare new tree to old tree to reject
empty commits. Always pass the old sha as `update-ref`'s third
argument - that makes the ref move a compare-and-swap, so a concurrent
writer gets a clean error instead of silently clobbering. Fire commit
observers (UI refresh hooks) after, and never let an observer
exception break the commit.

A branch only exists once it has a commit, and an empty repo breaks
everything downstream (pickers, clones, agents). Seed new repos with a
root commit - an empty one is fine: `mktree` with empty stdin →
`commit-tree <tree> -m "Initial commit"` → `update-ref
refs/heads/<b> <sha>`.

## Create / import / clone / branches

- Create: `init --bare --initial-branch=<name> <path>`, then the seed
  commit above (or a README via `commit_files`).
- Import an existing repo: `clone --bare <src> <dest>` mirrors all
  refs - then ALWAYS `remote remove origin`, because a bare clone's
  origin points at the local source path; re-add the source's real
  remotes only on explicit opt-in, so an imported repo can never
  accidentally push anywhere. Preserve its default branch with
  `symbolic-ref HEAD`.
- Import a plain directory: init a bare repo, then run `add -A .` with
  `GIT_DIR=<bare>`, `GIT_WORK_TREE=<srcdir>`, and a temp
  `GIT_INDEX_FILE`, followed by `write-tree` / `commit-tree` /
  `update-ref` - one root commit of the directory contents, source
  untouched.
- Clone from a URL: `clone --bare <url> <dest>` with the network env
  below; on failure delete the half-made dir. After any bare
  clone/remote add, set `remote.<name>.fetch` to
  `+refs/heads/*:refs/remotes/<name>/*` - bare clones don't get
  tracking refs by default, and fetch/ahead-behind need them.
- Branches: `branch <new> <from-ref>` to create, `branch -D` to
  delete - but refuse to delete the default branch. Deleting a repo
  needs typed-name confirmation; it's `rm -rf` with no undo.
- Fetch/push: `fetch --prune <remote>` and `push <remote> <branch>`,
  both with the network env and long timeouts.

For merges between depot branches, don't reimplement merge machinery:
make a throwaway `clone --shared` of the bare repo (objects via
alternates - cost is checkout only), run the real `merge` there, and
push the result back; the push doubles as the race guard, since the
bare repo rejects a non-fast-forward if the branch moved mid-merge.
Conflict handling and the resolution-branch pattern are in
git-local-sync.md; long-lived shared-clone checkouts for shell
execution are in exec-checkouts.md.

## Network ops that can never block - the askpass broker

`git fetch` over ssh with an encrypted key, or HTTPS needing
credentials, prompts on a TTY a desktop app doesn't have. Route every
prompt through the app instead:

1. Write a tiny helper script to `~/.yourapp/bin/askpass-helper`
   (chmod 0755) at startup. It takes the prompt as `argv[1]`, connects
   to a unix socket named in an env var, sends one JSON line
   `{"prompt": ...}`, blocks for one JSON line back, prints the secret
   to stdout on `{"ok": true}`, exits 1 otherwise. Give the socket a
   generous client timeout (~180 s).
2. Network git calls get `network_env()`: `GIT_ASKPASS` and
   `SSH_ASKPASS` pointing at the helper, `SSH_ASKPASS_REQUIRE=force`
   (OpenSSH ≥ 8.4 - use askpass even with a TTY), the socket path in
   your own env var, and `DISPLAY` set to something (`:0`) because
   some ssh builds ignore `SSH_ASKPASS` without it. Add
   `GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new` so
   first-time hosts flow; stricter prompts still surface via askpass
   as confirmation text.
3. The app runs a `ThreadingUnixStreamServer` on a socket under
   `run/` named per-pid, chmod 0600. Each helper connection becomes a
   broker request: check an in-memory cache keyed by prompt text;
   otherwise generate an id, store a `threading.Event` record, push a
   `prompt` event to the frontend, and `event.wait(timeout)` slightly
   below the helper's timeout (~170 s).
4. The user answers a modal; the UI thread calls
   `broker.answer(id, secret, remember)`, which fills the record and
   sets the event. Cancel = secret None = helper exits 1 = git fails
   with a normal error instead of hanging.

Answers marked "remember" go in the in-memory cache only - never on
disk - keyed by the exact prompt text, so one passphrase entry covers
a whole fetch/push burst and later calls that session; provide a
"forget secrets" action that clears it. This same broker is what makes
tunneled remotes usable - see http-over-ssh.md.

## Rules

- Never run clone/fetch/push without `network_env()`; never run ANY
  git without `GIT_TERMINAL_PROMPT=0`. A hung subprocess waiting on a
  hidden prompt is the failure mode you are designing against.
- Validate every user-supplied path (`..`, leading `-`) and sha before
  it reaches argv; ids are validated before path join.
- Every `update-ref` that moves an existing branch passes the expected
  old sha - CAS or nothing. Racing writers must error, not clobber.
- Never touch a repo's real index; worktree-less commits always set
  `GIT_INDEX_FILE` into a temp dir.
- Timeouts on every subprocess call; caps on every payload returned to
  the UI (file bytes, patch bytes, log count).
- After `clone --bare`, remove the path-pointing `origin` and set
  explicit `remote.*.fetch` refspecs for kept remotes.
- Secrets live in the broker's memory cache only; sockets are 0600 in
  a 0700 run dir; config with keys is 0600, written via unique temp
  file + atomic rename under a lock.
- Destructive operations (repo delete) require the user to retype the
  name; never delete the default branch.
