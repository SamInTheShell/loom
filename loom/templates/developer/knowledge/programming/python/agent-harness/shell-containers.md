# Shell containers - a sandboxed runtime per chat

The shell tool never runs a command on the host. Every chat gets one
long-lived container that idles on `sleep infinity`; each command is a
`podman exec` (or `docker exec`) into it, so after the first command
execs are ~instant - no per-command container startup, and package
caches, running daemons and warm state survive between commands. This
file is the runtime layer: detection, image provisioning, lifecycle,
mounts, streaming exec with real cancellation, and cleanup that never
leaks a container. The filesystem the container sees comes from
exec-checkouts.md; the tool that calls into all of this is described
in tool-catalog.md.

The security model in one sentence: the container gets a working tree
and a private home, and NOTHING else - no git metadata, no
credentials, no ssh keys, no host home. git never runs inside the
container (see below for why that is an invariant, not a preference).

## Runtime detection and forcing

Probe `podman` then `docker` with `shutil.which`; first hit wins
(podman preferred: rootless by default, daemonless). Read the version
with `<rt> --version` under a 5 s timeout - a hung daemon must not
hang your UI; a failed version check is reported, not fatal. Settings
may force one runtime; if the forced runtime is not on PATH that is a
hard error ("forced to docker but docker is not on PATH"), never a
silent fallback. Detect rootless once and cache it:

```python
# podman: info --format '{{.Host.Security.Rootless}}'  -> "true"
# docker: info --format '{{.SecurityOptions}}' -> contains "rootless"
```

Rootless matters for uid mapping on bind mounts (see User identity).
Run every runtime CLI call through one helper that captures
stdout+stderr combined, applies a timeout, and returns
`(returncode, output)` instead of raising - the runtime being broken
is a user-facing condition, not an exception path.

## Image profiles

A profile is a named image recipe: either a plain `image:` reference
to pull, or a custom Dockerfile that builds into a stable local tag
`<app>/<profile-id>:latest` (Dockerfile wins when both exist). A
profile may also carry an optional `bashPath` (for images that keep
bash somewhere unusual) and an optional uid/gid override. Keep
profiles as data (directories with a `Dockerfile` and/or
`config.yaml`), hash the source text, and mark a profile
"needs rebuild" when its hash changes - never rebuild implicitly on
the shell hot path.

## Provisioning - streamed, validated

Provision lazily, the moment a never-built profile is first used, and
stream every line of progress to the user (pulls and builds take
minutes; a silent spinner is unacceptable). `Popen` with
`stderr=STDOUT`, iterate lines, forward each through a callback; a
cancel `threading.Event` checked per line kills the process.

- Dockerfile profiles: write the Dockerfile to a temp dir, build with
  an EMPTY context directory (`build -t <tag> -f Dockerfile ctx/`) -
  the Dockerfile must be self-contained; `COPY`/`ADD` have nothing to
  copy, so a profile can never smuggle host files into an image.
  Always rebuild Dockerfile profiles on provision so edits take
  effect (layer cache makes no-op rebuilds fast).
- Plain profiles: pull only if absent. Presence check: podman
  `image exists <img>`, docker `image inspect --format ok <img>`.

Then VALIDATE: the whole exec layer speaks bash, so reject sh-only
images (alpine, distroless) at provision time with a clear error, not
at first command with a cryptic one:

```
<rt> run --rm --entrypoint bash <image> -c 'echo __bash_ok__'
```

Invoke a bare `bash` so it resolves against the IMAGE's PATH -
`/bin/bash` on Debian, `/usr/local/bin/bash` on the official bash
image; hardcoding a path rejects valid images. `bashPath` is the
escape hatch, not the default.

## One container per chat

Name: `<app>-<chat-id>` (sanitize the id to `[a-zA-Z0-9_.-]`, cap at
~60 chars - runtimes limit name length). Create with:

```
<rt> run -d --name <app>-<chat-id> \
  --label <app>=1 --label <app>.chat=<chat-id> \
  --label <app>.cfghash=<hash> \
  -v <tree>:/workspace:Z -v <home>:/agent-home:Z \
  -e HOME=/agent-home -w /workspace \
  --entrypoint bash <image> -c 'sleep infinity'
```

`:Z` keeps SELinux hosts working. `<tree>` and `<home>` come from the
chat's checkout (exec-checkouts.md). That is the ENTIRE mount set for
a shell container; a separate env-kind container (`<app>-env-<id>`)
adds read-only secret-file mounts and is the only place secrets ever
appear (secret-envs.md).

Reuse before create: `container inspect --format
'{{.State.Running}} {{index .Config.Labels "<app>.cfghash"}}
{{.Config.Image}}'`. Running + same image + same config hash → reuse
(touch the idle timestamp and return). Anything else - stopped, image
changed, mounts changed, user override changed - `rm -f` and
recreate. The config hash fingerprints everything that can only
change by recreation: extra mounts and their contents, uid/gid
arguments, identity env. Fold ALL of it into one short sha256 stored
as a label; comparing labels is the whole "does this container still
match its definition" check.

`run -d` can return success while the container dies instantly (bad
entrypoint, missing bash, OOM). Verify `{{.State.Running}}` is
`true` right after; if not, grab `logs --tail 5` for the error
message, `rm -f`, and raise with those logs - "exited immediately: "
plus the real reason.

## What is deliberately NOT mounted

The tree mounted at /workspace carries NO usable git metadata: the
checkout keeps its git dir outside the tree and the harness deletes
the `.git` pointer file (exec-checkouts.md). No host `~`, no
`~/.ssh`, no `~/.gitconfig`, no credential helpers, no docker/podman
socket. Consequences, which are the point:

- git never runs inside the container. All git operations -
  checkpointing, sync, push - run on the HOST with explicit
  `--git-dir`/`--work-tree`. A hostile or confused command inside the
  container cannot rewrite history, delete branches, install hooks,
  or read reflogs, because from its point of view /workspace is just
  files.
- Even if a container command plants its own `.git` in /workspace,
  host git ignores it (explicit dirs) and the harness unlinks it on
  the next sync. Hooks and config live in the host-owned git dir the
  container cannot reach.
- Nothing secret-shaped exists to exfiltrate. A prompt-injected
  `curl $(cat ~/.ssh/id_ed25519)` finds an empty home.

## Exec - streaming, cancellation, timeout

Each command runs as:

```
<rt> exec -w /workspace [--env K=V ...] <name> bash -lc <script> <argv...>
```

where `<script>` is the user command prefixed with one line:

```
echo "$$" > /tmp/.exec-<id>.pid
<command>
```

`$$` is the exec'd bash; in non-interactive mode its children share
its process group, so that one pid file is enough to kill the entire
tree later. `<id>` is unique per exec (millisecond timestamp works).
Caller-supplied arguments go AFTER the script as `$0` + positional
parameters (`"$1"`…), and per-exec env goes through `--env` - never
interpolate caller data into the command string, and never bake env
values into the container config where they would persist.

Stream stdout+stderr combined line-by-line through a callback. Cap
captured output (200 kB is plenty); past the cap, KEEP READING and
discard - stopping the read lets the command block forever on a full
pipe. A watcher thread wakes every 250 ms and checks two conditions:

- cancel event set → abort as "cancelled",
- monotonic deadline passed → abort as "timed out". Clamp the
  timeout: default 300 s, floor 5 s, ceiling 3600 s.

Abort must kill INSIDE the container - killing the client-side
`exec` process leaves the command running in the sandbox:

```
p=$(cat /tmp/.exec-<id>.pid); [ -n "$p" ] && \
  { kill -TERM -- -"$p" || kill -TERM -- "$p"; }
```

(negative pid = whole process group; fall back to the single pid).
TERM, wait ~2 s, then the same with KILL, then kill the local exec
client. Remove the pid file afterwards. Return a structured result:
`{exit, output, truncated, cancelled, timedOut}` - the caller decides
how to phrase each state.

## Reaping and never-leak cleanup

Touch a `{name: time.monotonic()}` map on every ensure/exec. A
daemon reaper thread ticks every 60 s and `rm -f`s containers idle
longer than 30 min (`IDLE_REAP_S = 1800`) - an abandoned chat must
not hold a container forever. Recreation is cheap because the tree
and home are bind mounts: the next command just gets a fresh
container over the same state.

The in-memory map dies with the process, so belt AND suspenders via
the label: at app exit AND at the next startup, run
`ps -aq --filter label=<app>=1` on BOTH runtimes that exist on PATH
(the user may have switched runtimes since the leak) and `rm -f`
everything found. A crash or SIGKILL can leak containers until the
next launch - never past it. Also remove a chat's containers (shell
and env kinds) when the chat is deleted. All cleanup paths swallow
errors: shutdown must never fail because a container was already
gone.

## User identity

Default: the container runs as the IMAGE's default user. Creating a
non-root user and setting `USER` is the Dockerfile's job - the
starter profile does `useradd -m -u 1000 agent` + `USER agent`. A
profile may force a specific uid/gid for ITS containers (the right
identity depends on the image, so this is per-profile, never
global):

- rootful, or uid 0: `--user <uid>:<gid>`.
- rootless podman, non-zero uid:
  `--userns keep-id:uid=<uid>,gid=<gid> --user <uid>:<gid>` - the
  chosen in-container uid maps back to the HOST user on bind mounts,
  so files the command writes stay owned by the host user and
  host-side git keeps working. Without keep-id, rootless bind-mount
  writes come back owned by a subordinate uid and the host can no
  longer commit them.

An overridden uid usually has no passwd entry in the image; podman
would then write the HOST username into the container's
`/etc/passwd` - a host-identity leak. Add a generic entry instead
(`--passwd-entry 'agent:*:<uid>:<gid>::/agent-home:/bin/sh'`) plus
`-e USER=agent -e LOGNAME=agent` (docker injects no passwd entry;
the env vars cover it there). Fold the user arguments into the
config hash so changing the override recreates the container.

## Rules

- git, credentials, and ssh keys never enter the container - the
  mount set is the security boundary; audit every `-v` you add.
- Never trust `run -d`'s exit code alone; verify Running and surface
  the container's logs when it died at birth.
- Kill via the in-container pid file (process group first), not just
  the client-side exec process - otherwise "cancelled" commands keep
  running in the sandbox.
- Keep draining output after the truncation cap; a full pipe
  deadlocks the command.
- Cleanup is label-driven and runs at exit AND startup, on every
  runtime present - the in-memory bookkeeping is an optimization,
  the label is the guarantee.
- Validate bash at provision time; reject sh-only images with an
  actionable error naming `bashPath` as the fix.
- Build Dockerfile profiles with an empty context so images can
  never absorb host files.
- Per-exec env via `--env`, caller args via positional parameters -
  no interpolation, no persistence.
