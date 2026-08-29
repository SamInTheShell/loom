# Secret environments — user-defined tools with credentialed material

An "environment" lets the user hand an agent curated, credentialed
capabilities without ever exposing the credentials: it bundles a
container profile, variables and files (each plain or secret), and
PSEUDO TOOLS — name + description + typed params + a command — that
the model calls exactly like built-ins (tool-catalog.md). The design
problem is entirely about secrets: where values rest, who may read
them, when they materialize, and what the free-form shell can see.
Get one of those wrong and every agent transcript is a credential
leak. Execution mechanics of the containers themselves are
shell-containers.md; the agent loop that carries the calls is
agent-loop.md.

## The structure / value split

An env definition is ordinary config — EXCEPT secret values, which
never rest in the app's config directory at all:

- config holds the STRUCTURE: env name, container profile id, tool
  definitions, variable names, file container-paths, non-secret
  values, and a boolean `has` marker per secret entry ("a value is
  stored somewhere").
- values of anything marked secret live in the OS keyring — the
  `keyring` package, which speaks Secret Service / KWallet on Linux
  (and the platform stores elsewhere) — one keyring item per entry,
  keyed like `service="appname"`, `key="env:<envId>:<var|file>:<name>"`.

Probe the backend once and cache the verdict; `keyring.get_keyring()`
happily returns a fail/null backend on headless boxes, so check the
backend class name for "fail"/"null" and record an error string
instead of a module. Surface `{available, backend, error}` to the
settings UI so the user learns "no keyring" at configure time, not
mid-task.

```python
def kr_set(env_id, kind, key, value):
    s = probe()                      # cached {mod, backend, error}
    if s["mod"] is None:
        raise EnvError(
            f"cannot store secret '{key}': {s['error']}. Secrets "
            "require an OS keyring — refusing to store plaintext.")
    s["mod"].set_password(SERVICE, f"env:{env_id}:{kind}:{key}", value)
```

That raise is the no-silent-downgrade rule: with no usable backend,
saving a secret FAILS loudly. Never fall back to writing the value
into config "temporarily" — a secret that once touched disk in
plaintext is burned.

## Write-only from the frontend

The UI can set or replace a secret; it can never read one back:

- Listing envs returns a SANITIZED shape: secret vars carry
  `value: None, has: true/false`; secret files carry `has` and a byte
  size. The value key simply does not exist in what crosses the
  backend↔page bridge.
- Saving takes two arguments: the structural definition, plus a
  separate `secrets = {"vars": {KEY: value}, "files": {fileId:
  content}}` holding ONLY newly-entered values. An absent key means
  "keep whatever the keyring has" — so the edit dialog round-trips
  the sanitized shape untouched and secrets survive unrelated edits.
- Housekeeping on save: an entry demoted from secret to plain deletes
  its keyring item; entries that disappeared delete theirs; deleting
  the whole env deletes every one. Keyring deletion is best-effort
  (backends have quirks), creation is not.

Validate structure at save: tool names `^[a-z][a-z0-9_]{0,39}$` and
never shadowing a built-in tool name (reject at save AND skip at
runtime — tool-catalog.md), param names likewise, variable names
`^[A-Za-z_][A-Za-z0-9_]{0,63}$`, file paths absolute inside the
container with no `..`, file content capped (e.g. 256 KiB — these are
credentials and small configs, not datasets).

## Exec-time resolution and tmpfs materialization

Secrets resolve ONLY at execution time, only in the backend — the
resolved bundle never crosses the JS bridge. Resolution returns
`{"def", "vars": {K: V}, "files": [{path, content, secret}]}` and
raises naming exactly what is missing ("secret variable 'TOKEN' has
no stored value — set it in settings") — a clear error, never a
silent empty string that turns into a confusing auth failure three
layers down.

File material is written fresh on every call to a per-chat directory
under `XDG_RUNTIME_DIR` — tmpfs: RAM-backed, never touches disk,
wiped at logout — falling back to a 0700 run dir inside the app's
home only when the runtime dir is absent. The directory is chmod
0700, each file 0600, and each is bind-mounted READ-ONLY at its
declared container path. Hash the (path, content) pairs into a short
digest and fold it into the container's config hash: when the user
edits the env, the stale container is torn down and recreated instead
of serving old secrets.

```python
d = runtime_dir / "envfiles" / chat_id      # tmpfs
shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
os.chmod(d, 0o700)
for i, f in enumerate(resolved["files"]):
    host = d / f"f{i}"
    host.write_bytes(f["content"].encode())
    os.chmod(host, 0o600)
    mounts.append((str(host), f["path"]))    # mounted :ro
```

## A separate container from the free-form shell

Each chat runs TWO container identities from the same checkout: the
shell container (the agent's arbitrary `bash -lc` commands) and the
env container (only env pseudo tools). Env vars and file mounts are
injected only into the latter. The reason is the whole point of the
feature: the free-form shell is model-authored arbitrary code; if it
shared a container with the secrets, `cat $TOKEN_FILE` ends up in the
transcript and the provider's logs. The env container instead runs
only user-authored commands — the model chooses arguments, never the
program. Name them distinctly (`shell-<chat>` / `env-<chat>`) and
reap them on chat deletion together with the materialized files.

## Pseudo tools become real tools

Each tool definition converts into a provider-facing spec exactly
like a built-in: typed params (`string`/`integer`/`boolean`, anything
else coerced to string), required flags, and a description prefixed
with the environment's name so the model knows which bundle it is
invoking. Execution runs the user's command with the model's
arguments passed out-of-band — NEVER interpolated into the command
string:

- each declared param becomes an environment variable
  (`ARG_<NAME>`, uppercased) AND a positional argument;
- the stored command gets ` "$@"` appended and runs under bash, so
  `$1`, `$2`, … are the params in declaration order;
- undeclared arguments are dropped; missing required ones fail before
  anything runs.

This is injection-proof by construction: the model influences argv
values, never the parsed command line. After the command finishes,
any file changes in the checkout are auto-committed like every other
mutation (tool-catalog.md, git-depot.md) with a message naming the
env and tool — so a credentialed deploy script is a rollback point
like any edit. Env tools require a repository attachment's branch;
they do not apply to directory attachments (there is nothing to
commit to). Permission-wise the whole family shares one perm id
(`env.tool`) gated by the frontend like everything else.

## Failure posture

Every failure path names its cause and refuses to degrade:

- no keyring backend → saving a secret errors; nothing is written.
- stored value missing at exec (keyring wiped, item deleted) → the
  tool call fails with the entry name and the keyring error, telling
  the user where to re-enter it.
- env deleted while a chat still references it → "environment not
  found — pick another", not a crash and not an empty env.
- container profile missing/broken → error before any secret is
  materialized.

The invariant behind all four: at no point does the system substitute
an empty/plaintext/stale value to keep going. Secrets either flow
correctly or the call fails visibly.

## Rules

- Secret values rest ONLY in the OS keyring; config carries structure
  and `has` markers. Grep your config file for a known secret in
  tests — it must never appear.
- The frontend is write-only for secrets: sanitized reads, delta-only
  writes, absent key = keep. Resolution happens at exec time in the
  backend and never crosses the bridge.
- Materialize to tmpfs, dir 0700, files 0600, mounted read-only,
  rewritten per call; fold a content digest into the container hash
  so edits recreate the container.
- Env secrets exist in the env container only — never in the chat's
  free-form shell container.
- Params reach commands as env vars + `"$@"` positionals; never
  build a command line by string interpolation of model input.
- No silent downgrade, no silent empty: every secret failure is a
  loud, named error at the earliest possible moment.
- Clean up on delete: keyring items, materialized files, containers.
