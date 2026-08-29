# Agent coding harness — the assembly map

This folder is a component catalog for building a desktop AI coding
workbench in Python: a pywebview app where the user chats with agents
that read and edit git-backed projects, run shell commands in
containers, and call user-defined tools — every step recorded as a
commit, every risky action permission-gated. Each file covers one part
with concrete contracts; this README is the map that shows how they
compose.

## The layer diagram

```
        webview page (../../web/js-app-architecture.md)
              │  JsApi calls ▲ · ▼ pushed events, blocking gates
ui-bridge.md            the Python↔JS event/gate plumbing
persistence.md          config.json, transcripts, migrations
              │
agent-loop.md           the agentic turn loop (thread per message)
streaming-cursor.md     deltas → markdown re-render → cursor
              │
llm-providers.md        provider instances + per-API SSE adapters
virtual-providers.md    priority/capacity routing over instances
http-over-ssh.md        reach remote providers with no local port
              │
tool-catalog.md         the flat tool catalog, modes, permissions
secret-envs.md          user-defined pseudo tools + keyring secrets
search-index.md         FTS5 + vector index over transcripts/code
attachments-extraction.md  staging user files, safe doc extraction
              │
git-depot.md            bare-repo depot, worktree-less commits
git-local-sync.md       depot ⇄ user working copies, ff-only
exec-checkouts.md       per-chat checkouts, checkpoint commits
shell-containers.md     container-per-chat sandboxed shell
```

## Configurations you can assemble

1. **Chat client** — ui-bridge.md + persistence.md + llm-providers.md
   + agent-loop.md + streaming-cursor.md: streaming multi-provider
   chat with no tools. Everything later plugs into this loop.
2. **Coding agent** — add git-depot.md + tool-catalog.md: agents read
   and edit bare-repo projects, every mutation a commit.
3. **Sandboxed shell** — add exec-checkouts.md + shell-containers.md:
   a real filesystem and a container per chat, checkpoint commits
   after every command.
4. **Full workbench** — add virtual-providers.md, secret-envs.md,
   search-index.md, attachments-extraction.md, git-local-sync.md,
   http-over-ssh.md: routing/failover, user tools with secrets,
   hybrid search, file attachments, syncing to on-disk clones, and
   remote providers over SSH.

## Reading order for building

1. `../pywebview/` — the app shell basics (window, JsApi, file://).
2. `ui-bridge.md`, `persistence.md` — plumbing every feature rides on.
3. `llm-providers.md` → `agent-loop.md` → `streaming-cursor.md` — a
   working streaming chat.
4. `git-depot.md` → `tool-catalog.md` — agents that do things.
5. `exec-checkouts.md` → `shell-containers.md` — the shell tool.
6. The rest as features demand.

## Rules that keep the parts composable

- **The frontend owns policy, the backend owns mechanism.** Permission
  decisions, chat metadata, and mode config live page-side; backend
  threads block on gates and never decide for the user
  (ui-bridge.md, agent-loop.md).
- **Every mutation is a commit** — tool writes, shell checkpoints,
  editor saves all land as commits on a branch of a bare repo; there
  is no unsaved state to lose (git-depot.md, exec-checkouts.md).
- **Nothing automatic touches the user's own files or secrets.**
  Local-folder sync is click-only (git-local-sync.md); secret values
  rest only in the OS keyring and tmpfs (secret-envs.md).
- **Containers see the tree, never the credentials** — no git dir, no
  keys, no host network assumptions inside (shell-containers.md).
- **Evidence over assertion in UI state**: providers are "connected"
  only after a real probe; counts shown are counts measured
  (llm-providers.md, search-index.md).
