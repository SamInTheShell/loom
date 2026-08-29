# Tool catalog — one flat catalog, modes, scoping, and result shapes

How to design the tool surface of an agentic coding workbench: every
tool the app can ever offer lives in ONE flat catalog of declarative
specs, and a chat's MODE is nothing but a named subset of that catalog.
This file covers the catalog data model, attachment scoping with
single-target resolution, the read/mutate asymmetry, directory
attachments with hard path confinement, the app-wide coordination
tools, permission gating, and the result-shape conventions the whole
UI depends on. The loop that drives calls is agent-loop.md; command
execution is shell-containers.md; the commit model is git-depot.md.

## The catalog data model

A tool spec is a plain dict — no classes, no decorators — so the same
list drives provider payloads, the mode editor, and permission gating:

```python
{"name": "read_file", "perm": "fs.read", "mutating": False,
 "description": "...",                      # written FOR the model
 "params": {"repo": REPO_PARAM,             # JSON-schema properties
            "path": {"type": "string", "description": "..."}},
 "required": ["path"]}
```

`perm` is a small permission id (`fs.read`, `fs.edit`, `git.read`,
`shell.exec`, …) shared by related tools — the frontend maps ids, not
tool names, to Ask/Allow/Deny policy. `mutating` drives both the
target-resolution rules below and the "auto-accept edits" tier.
Convert specs to each provider's shape mechanically (OpenAI
`{"type": "function", "function": {...}}`, Anthropic `input_schema`,
Gemini `function_declarations`) — one list, three trivial adapters.

The catalog has two halves: repository tools scoped by the chat's
attachments, and app-wide tools (below) that ignore attachments. Both
halves are one namespace; an API endpoint serves the merged list with
a `group` per tool so the mode editor can render sections.

## Modes — named subsets, no mode means no tools

A chat mode is `{id, name, scope, tools: [names]}` stored in config.
The loop filters: `[t for t in ALL_SPECS if t["name"] in enabled]`.
Rules that make this honest:

- No mode (or an empty selection) sends the model NO tools, and the
  system prompt says so explicitly: "you cannot read repositories or
  run commands; never claim to have checked something."
- The composed system prompt describes only enabled tools and names
  the disabled ones, so the model never promises capabilities it
  lacks. Compose the guidance per-mode, not one static blob.
- `scope` is `attached` or `depot` — it widens READ tools only (next
  section). Capabilities that aren't single tools (e.g. enabling an
  environment's pseudo tools, secret-envs.md) appear as a pseudo-name
  in the selection (`env_tools`).
- Presets ("agent" = code work, an app-wide assistant mode, a
  read-everything-change-nothing researcher, "chat only" = no tools)
  are ordinary editable modes, not special cases in code.

## Attachments and single-target resolution

A chat carries an ordered attachment list; two shapes coexist:
`{repoId, branch|None, mode}` for depot repositories and
`{dir, name, mode}` for local folders, `mode` being `read` or `edit`.
Every repo-touching tool takes an optional `repo` string argument that
selects an attachment by exact name. Resolution:

1. `repo` given → that attachment, or a precise error listing what IS
   attached (models recover well from errors that enumerate options).
2. No `repo`, mutating → the first attachment with edit access, else
   an error telling the model to ask the user to flip one to edit.
3. No `repo`, read → the PRIMARY attachment: first editable, else
   first attached.
4. `grep` with no `repo` is the one multi-target read: it sweeps every
   attachment, prefixing each match with the attachment name.

The resolved target rides on EVERY result (`repo` + `branch` keys) and
is the first row of every approval preview, never truncated — an
approval must be unambiguous about where it acts. A repository
attachment resolves to one ref: its pinned branch when it still
exists, else the repo's default branch — for reads. If a MUTATING
call's pinned branch has vanished, refuse with an error; never
silently redirect a write to the default branch.

## Read vs mutate asymmetry

Reads and writes have deliberately different reach:

- A `depot` mode scope lets READ tools name ANY depot repository in
  `repo` (resolved read-only on its default branch), and `grep` with
  no `repo` sweeps the whole depot. Discovery goes through a
  `list_repos` tool.
- Mutating tools NEVER widen. A write requires an attachment with
  edit access, full stop — the attachment list is the user's write
  grant and no mode setting overrides it.

Read tools run against git refs directly (`ls-tree`, `cat-file`,
`git grep <ref>`) on the bare depot repo — no worktree, so reads are
cheap, parallel-safe, and always consistent with a single ref
(git-depot.md). Budget each read against the model's context window
(a fraction like 0.2 of `ctx_tokens × ~4 chars/token`, clamped to
sane floors/ceilings); oversized files come back as a line range with
an explicit "call again with start_line=N" hint so the model pages.
Images return as real image content — the result carries
`{"image": {"mime", "b64"}}` and the loop appends a provider-native
image block; a text placeholder tells non-vision models honestly that
they cannot see it. PDF/Office files are served as safely extracted
text, and grep searches that same extracted text so a document match's
line number feeds straight into `read_file` (search-index.md handles
semantic search; this is the exact-match layer).

## Every mutation is a commit

`write_file`, `edit_file`, `rm` each produce one commit on the target
attachment's branch; `shell` auto-commits whatever file changes a
command leaves behind. Every agent action is therefore a rollback
point with an attributable message — no staging area, no "dirty state
the user must untangle". `edit_file` picks its mode from which params
arrived: `old_string`+`new_string` (must occur exactly once),
`start_line`+`end_line`+`new_string`, or `content` for a full rewrite.
Share the edit-application function between execute and the approval
preview so the committed change always equals the previewed diff.

## Directory attachments — hard confinement

A local folder attaches with the same tool contract except there is no
bare repo behind it: writes land DIRECTLY on disk, no commits, and
every mutating result says so ("no commit — not rollbackable").
Confinement is non-negotiable, on reads and writes alike:

```python
def resolve(root: Path, rel: str) -> Path:
    rel = (rel or "").strip("/")
    if rel.startswith(("-", ":")) or ".." in rel.split("/") \
            or Path(rel).is_absolute():
        raise ToolError(f"invalid path: {rel!r}")
    target = (root / rel) if rel else root
    base, resolved = root.resolve(), target.resolve()
    if resolved != base and base not in resolved.parents:
        raise ToolError(f"path escapes the attached directory: {rel!r}")
    return target
```

Note the details: leading `-` blocks flag injection into git argv;
leading `:` blocks git pathspec magic (`:(literal)`, `:/`) that would
smuggle traversal past the checks; `resolve()` catches symlinks that
point outside the root. Path FILTERS (grep/log prefixes) go through
the same check without requiring existence. On top of confinement,
writes refuse protected directories (`.git`, `.hg`, `.svn`, `.venv`,
`venv`) — host state agents must not corrupt — while reads of them
stay allowed; the shell container bind-mounts each protected dir
read-only over the workspace mount for the same guarantee
(shell-containers.md). Recursive delete refuses the attachment root,
symlinked directories, and any subtree containing a protected dir.

The read-only git tools work against the folder's OWN `.git` when it
has one — but an untrusted repo's config must never execute anything:
force `-c core.hooksPath=/dev/null -c core.fsmonitor=false
-c core.pager=cat -c core.sshCommand=/bin/false` on every invocation.
Shell still runs inside the chat's container with the folder mounted
at the workspace path; commands never touch the host directly.

## App-wide tools — read and coordinate

The second half of the catalog ignores attachments and reads APP
state: `list_repos` (depot inventory), `overview` (every chat with
its rw/ro access, grouped by the repos it can write to, with status),
`read_chat` / `search_chats` (persisted transcripts), and
`view_settings` — SANITIZED: provider entries expose enabled state
and models but never API keys, environments expose names only. Two
rules keep this half safe and useful:

- Self-exclusion: the calling chat's id travels in the tool context;
  overview/read_chat/search_chats never show a chat its own
  transcript (it has its history), and `send_message` refuses to
  message the sender. Without this, models loop on themselves.
- Acting tools (`send_message`, `create_chat`) mutate state the
  FRONTEND owns (the chat list is the page's source of truth), so
  they execute IN the frontend: the loop emits a `tool_exec` event
  and blocks until the page posts the outcome back. Backend and
  frontend each execute only what they own. A relayed message always
  carries a visible "via" marker — the receiving agent and the user
  both see it was not typed by the human.

Per-chat "context omission" (hiding repos from inventories and sweeps)
is a signal-to-noise control for small models, NOT a security
boundary: a direct read of an omitted repo still works. Say so in the
docstring or someone will treat it as an ACL.

## Permission gating and result shapes

The backend never decides permission. For each call the loop registers
a gate, emits a `tool_call` event carrying the perm id, the resolved
target, and a side-effect-free PREVIEW (param rows, unified diff for
edits, a note — including "This call will FAIL: …" when resolution
already failed), then blocks until the frontend answers Ask/Allow/Deny
from its policy plus per-chat overrides. Register the gate BEFORE
emitting or a fast answer races the wait. A denial is not an
exception: it becomes a normal `{ok: False}` result fed back to the
model so the loop continues gracefully.

Every executor returns the same JSON-ready dict and NEVER raises:

```python
{"ok": bool, "summary": "one line for the collapsed card",
 "detail": "full text for the model", "diff": [...]|None,
 "sha": "...",            # when a commit was produced
 "repo": "...", "branch": "...",   # the resolved target, always
 "image": {"mime", "b64"}}         # image reads only
```

Wrap dispatch in a broad except that converts any exception into an
error result — one tool bug must never kill the agent loop. Truncate
loudly, never silently: every clipped grep/log/diff/read ends with an
explicit marker and a continuation hint (`start_line=N`, `skip=N`,
"pass path to narrow it").

## Rules

- One flat catalog; modes select, code never branches on "which kind
  of chat is this". No mode = no tools, and the prompt admits it.
- Writes require an edit attachment; no scope, flag, or default ever
  widens a mutating tool. Reads may widen to the whole depot.
- Echo the resolved target on every result and every approval; a
  vanished pinned branch fails writes instead of redirecting.
- Confinement checks run on reads AND writes; reject `-`/`:` prefixes,
  `..`, absolute paths, and out-of-root symlink resolution.
- Foreign `.git` configs are hostile: disable hooks, fsmonitor,
  pager, and ssh on every git invocation in a user directory.
- Frontend executes what the frontend owns; backend executes the
  rest; neither decides permission alone — the frontend resolves
  Ask/Allow/Deny, the backend only enforces "no answer, no run".
- User-defined pseudo tools (secret-envs.md) join the same catalog
  but may never shadow a built-in name — validate at save AND skip at
  runtime (belt and suspenders).
- Never let a tool raise into the loop; never truncate without a
  visible marker and a way to continue.
