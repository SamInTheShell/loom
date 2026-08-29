# Persistence without a database — one owned dir, JSON, migrations

Everything a desktop workbench needs to remember — config, chat
transcripts, notification history, user content — fits in plain JSON
and files under ONE app-owned directory. No SQLite, no ORM: the data
is small, single-user, and read by exactly one process (the
single-instance pidfile in ui-bridge.md guarantees that). What makes
it robust is a handful of disciplines: atomic writes, loads that
never raise, a small always-loaded index with lazy payloads, and
migrations that walk any old install's data forward on read. This
file is those disciplines; durable secrets are the one thing that
does NOT belong here (secret-envs.md).

## The data directory

```
~/.yourapp/
  config.json           app configuration (0600 — may hold API keys)
  workspace.json        chat/skills INDEX — metadata only, no bodies
  chats/<id>.json       one transcript per chat, loaded lazily
  notifications.json    ring-buffer event log
  repos/<name>.git      bare repositories (git-depot.md)
  workspaces/<chat>/    per-chat execution checkouts (shell-containers.md)
  bin/                  helper executables (askpass — ui-bridge.md)
  run/                  unix sockets, transient state (chmod 0700)
  yourapp.pid           single-instance gate
  yourapp.log           stdio of the detached process
```

One module owns path derivation: `data_root()` reads an env override
(`YOURAPP_HOME`) before defaulting to `~/.yourapp` — tests point it at
a temp dir and exercise real persistence with zero mocking. Every
other module asks that one for paths; `ensure_dirs()` is idempotent
and called before any write. Store paths in tilde form in JSON,
expand at the filesystem boundary. Never rename an on-disk directory
just because the concept it holds was renamed — absolute paths inside
existing data (git checkouts especially) break; keep the historical
name and note why in a comment.

## config.json conventions

One flat dict. Loading merges the file OVER a `DEFAULT_CONFIG`
constant, so new keys heal into old files automatically, and never
raises — a missing or garbled file yields the defaults:

```python
def load_config() -> dict:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})
    return _heal(merged)      # nested defaults, legacy sub-shapes
```

Saving is atomic and race-proof: write to a tmp file whose name
embeds pid AND thread id, `chmod 0600` (the config may hold provider
API keys), then `os.replace` onto the real path. An in-process lock
serializes writers (bridge calls, seeding, session tracking all write
concurrently); the unique tmp name means even another process can't
make the chmod/rename window race.

**Lists of user instances.** Anything the user can add several of
(provider accounts, environments) is a LIST of records under one key
— `providerList: [{id, type, name, baseUrl, apiKey, enabled,
lastProbe}]` — never a dict of fixed ids. Records carry their own
`id` (uuid); a projection function strips secrets before a record
crosses the bridge (`hasKey`, `keyHint: "…" + key[-4:]` — never the
key). Two instances of the same type is then free.

**Migrating legacy shapes in place.** Detect old shapes by SHAPE, not
by a version number: `providerList` missing while a legacy
`providers` dict exists means pre-instances data — convert the
entries the user actually touched, keep the old ids as instance ids
(references elsewhere keep resolving), then `pop` the old key so
exactly one source of truth remains. Run the check inside every load
of that list; save once when it fired. A subtler trick: leave one key
deliberately UN-defaulted so its absence marks a legacy install for a
one-time migration — healing it in `load_config` would erase the
signal.

Know per setting whether it's read at startup or per call: per-call
reads pick up on-disk edits live; startup reads need a restart
(../pywebview/debugging.md).

## Index vs payload split

A transcript-heavy app must not load every message at startup.
Split: `workspace.json` holds chat METADATA plus small collections
(skills); `chats/<id>.json` holds one full message array each,
loaded only when a chat opens, deleted with the chat.

Saving the index strips two things from each chat record: the
`messages` key, and every key starting with `_`. The underscore is a
CONVENTION for transient runtime state (live stream buffers, queued
sends) — strip by prefix, not by an allowlist, so a new transient key
can never leak to disk because someone forgot to register it. Apply
the same convention in the frontend's export path.

Both files use the atomic tmp+replace write. The per-chat file needs
the pid+thread-unique tmp name too: a stream finishing and a user
send can save the SAME chat concurrently. The index has its own lock
because chat-meta and skills are separate read-modify-write paths
that would clobber each other's key. Sanitize the chat id into the
filename (alnum plus `-_`, capped length). Derived stores (a search
index) update via a post-save hook wrapped in try/except — keeping an
index fresh must never block or break a save.

## Era-walking migrations

Old installs must load forever. One `_migrate(d)` runs inside every
index load and walks the data through EVERY historical shape, oldest
first, in place; the next save persists the final shape. Each step
keys on shape presence and is idempotent — new data falls through
untouched. A real chain, as a worked example of the technique:

```python
def _migrate(d):
    # era 1 -> 2: a grouping concept was renamed
    if "workspaces" not in d and isinstance(d.get("projects"), list):
        d["workspaces"] = d.pop("projects")
    # era 2 -> 3: the grouping layer was deleted; each chat now
    # carries what it used to inherit — resolve THROUGH the old
    # groups, then drop them
    spaces = {w["id"]: w for w in d.get("workspaces") or []}
    for c in d.get("chats") or []:
        wid = c.pop("workspaceId", None)
        c.setdefault("repoId", (spaces.get(wid) or {}).get("repoId"))
    d.pop("workspaces", None)
    # era 3 -> 4: two scalar fields fold into one list-of-records;
    # build the list from the old fields, then pop them — one
    # source of truth afterwards
    ...
    return d
```

Rules of the chain: `isinstance`-check everything (old files contain
surprises); resolve through removed concepts rather than dropping the
data they carried; after each step exactly one representation
remains; and keep the FILE name stable across eras — renaming
`workspace.json` to match the current concept would orphan every old
install. Write the era history as a comment above the function; it is
the only place that story survives.

## Convention-over-registration content discovery

Content the user authors should appear by EXISTING, not by being
registered. The pattern, worked for prompt files:

- Any `*.prompt.md` file in any user content repo (scanned on the
  default branch) IS a system prompt. Drop `reviewer.prompt.md` in —
  "Reviewer" appears in every picker; delete it — gone. No settings
  entry, no registry to desync.
- Its id is `"{repoId}:{path}"` — stable across restarts and
  human-readable where it lands in config.json.
- Content resolves LIVE at each use (read the file at send time, by
  id), so editing updates every chat that uses it immediately. Only
  the list of ids is scanned up front.
- Display name derives from the filename
  (`code-reviewer.prompt.md` → "Code Reviewer"); the file's first
  `# heading` becomes its description. Zero metadata files.
- The marker-directory exception: the same suffix under a `compact/`
  directory is NOT a system prompt — it is the instruction used when
  a chat compacts its context (a `title/` directory works the same
  for chat naming). Pairing is by filename: a chat running
  `system/dev.prompt.md` compacts with the sibling
  `compact/dev.prompt.md` when it exists; else a configured fallback
  id; else a built-in constant. Convention gives you scoped variants
  without any linking UI.
- Legacy pre-convention ids still resolve through the old config
  list — discovery arrives without breaking anyone.

The same shape recurs: container profiles are directories in a repo
(a commit hook re-syncs the list), docs pages are `.md` files in a
directory bundled at compose time (ui-bridge.md), ordered by an
optional `NN-` filename prefix that is stripped from the slug.

## Bounded ring-buffer logs

An append-forever JSON log eventually eats the disk and the load
time. The notification history caps BOTH dimensions at write time:

```python
def add(severity, source, text):
    entry = {"ts": int(time.time() * 1000),
             "severity": severity if severity in
                 ("info", "ok", "warn", "err") else "info",
             "source": str(source)[:60], "text": str(text)[:500]}
    with _lock:
        entries = load()                 # load() never raises
        entries.insert(0, entry)
        _save(entries[:MAX_ENTRIES])     # newest-first, e.g. 200
    return entry
```

Newest-first means readers take a prefix and the truncation slice is
trivial. Return the entry so the caller can push it to the page
(ui-bridge.md) without re-reading. Clear is `_save([])`.

## What belongs in the OS keyring instead

Two tiers. User-authored SECRETS (environment variables, credential
files handed to tools) never touch this directory: config keeps the
structure and a "secret" marker, the keyring keeps the value, and the
bridge is write-only for them — reads return existence, not content
(secret-envs.md is the full treatment, including the no-backend
failure mode). Provider API keys are the pragmatic exception living
in 0600 config — but they still never cross the bridge (the
projection above). If you can't show it in DevTools, it doesn't
belong in a bridge response.

## Rules

- Every write is tmp + `os.replace`, tmp name unique per pid AND
  thread; lock in-process writers per file.
- Loads never raise: missing/garbled → defaults or empty, and
  `isinstance`-check every field that came from disk.
- Merge defaults over loaded config on every read; heal nested
  shapes — except keys whose absence IS the legacy-detect signal.
- Index small and always loaded; payloads one file each, lazy.
  Strip `_`-prefixed transient keys by prefix, never by list.
- Migrate on read, keyed on shape, in place, idempotent, oldest era
  first; pop superseded keys so one source of truth remains; never
  rename data files or directories across eras.
- Discovered-content ids are stable and human-readable
  (`repo:path`); resolve content live at use, not at scan.
- Ring-buffer logs clamp count AND per-field size at write time.
- Secrets: keyring via secret-envs.md; nothing secret in a bridge
  response, ever.
- Point the whole tree at a temp dir via one env var and test
  against the real code paths (../pywebview/debugging.md).
