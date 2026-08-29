# Chats

A chat streams against one of your managed llama-servers over its unix
socket. The message area follows llama.cpp's web chat: your messages in
bubbles (nudged right), the model's as floating markdown (nudged left),
tool activity as collapsible cards between them, and a stats row
(tok/s, time-to-first-token, token counts) after each turn.

## Sending

- **Enter** sends, **Shift+Enter** breaks the line. **Up/Down** at the
  start of the input walk your message history; **PgUp/PgDn** scroll the
  thread without leaving the input; **Esc** stops a streaming reply.
  **Ctrl+I** focuses the input from anywhere; a waiting permission card
  answers to **Ctrl+Enter** (allow) / **Ctrl+Esc** (deny). Hold **Ctrl**
  to reveal every shortcut in place.
- A sent message moves to the **queue panel** above the input and
  dispatches when the model is running and the chat idle. Queue rows can
  be edited back into the input, force-sent, or cancelled. Sending while
  a reply streams just queues the next message.
- If the model isn't running, Loom offers to start it and fires the queue
  when it's ready.
- **Retry / Continue** appears under the thread when the last word was
  yours or a reply stopped early — both resume the loop in place.
- Closing a chat tab archives it (reopen from the Chat Archive tab); a
  streaming chat asks before cancelling. **Ctrl+N** clones the current
  chat's setup — model, permission mode, attachments — into a fresh
  context window.

## Tools and permissions

The model gets file and search tools over the knowledge base and
attached folders (`/mnt/<name>`), plus `shell`. What runs freely is the
**permission mode** — the shield pill top-right of the input panel:
`always-ask` (changes and shell confirm), `allow-edits` (edits and
sandboxed shell run without asking), `always-allow`, or your own defined
under `permission-modes:` in loom.yaml (levels per tool: allow / ask /
deny / disabled). Switching the mode also re-decides any tool call
already waiting for permission.

The security boundary is the **container**, not the tool list:

- **Shell always runs in a container** — specifically the one named by
  `containers.default` in loom.yaml (terminals can pick per tab; chats
  use the default) — as an unprivileged user, never on the host. View-mode
  folders and the knowledge base (`/knowledge`) are mounted read-only
  at the kernel level, so the shell is safe read-only tooling; flip a
  pill to *write* deliberately when you want edits.
- **Network is off by default.** The chip left of the model selector
  toggles container network access per chat (Ctrl+Shift+N); nothing in
  a library file can switch it on.

Two pills left of the permission mode steer the shell's world:

- **Container** — which `containers:` definition runs shell commands;
  chats start on `containers.default`, switchable per chat.
- **Environment** — a named env-var set loaded into shell containers
  (chats start with none). Definitions live in the library's
  `environments.yaml` — plain values plus SECRET STUBS; secret values
  live in your OS keyring, set per machine in the Environments tab
  (reachable from the pill). With network on, this lets the model drive
  cloud CLIs with real credentials — the model is told which variables
  are set, never their values.

## Artifacts

`/artifacts` is the chat's read-write delivery folder. Anything the
model leaves there arrives as a pill in the attach bar — files with a
**save** button, folders as **zip** downloads, images with hover
previews. Your uploaded images are copied to `/artifacts/uploads` so
shell commands can process them. Artifacts persist with the chat and
are purged when it is permanently deleted from the Archive.

## Reasoning effort

The **brain** button on each row of the model menu configures how hard
that model thinks: pick the method (`reasoning_effort` request field —
graded models like Qwen 3.8 take off/low/medium/xhigh; the boolean
`enable_thinking` template kwarg; or the `/think` prompt switch) and
the level. The model button shows the configured level at a glance;
**Default** sends nothing and lets the server decide.

Read tools show numbered, syntax-highlighted previews; edits show real
diffs — in the permission card too, so you approve what will actually
change. Resolved cards collapse to one line; click to inspect.

## Context and compaction

The **context chip** (left of the network toggle) shows usage; hover for
the breakdown — system prompt, messages, thoughts, tool results and
definitions, estimated total vs the model's real window, and the last
turn's actual token count.

When usage crosses `chat.compaction.threshold` (default 80%), the
conversation is summarized with `prompts/compaction.md` and the summary
replaces the older turns on the wire — the full history stays in the
file and on screen (a collapsible "context compacted" card marks the
seam). "Compact now" lives in the chip's hover card. Older thinking is
dropped from the wire by default (`chat.thought_truncation`); only the
latest turn's thoughts ride along.

## Attachments

- **Images** (png/jpeg) attach via the image button — they reach the
  model when it runs with an `mmproj` projector (see
  [inference tips](inference-tips.md)).
- **Folders** attach in view (read-only) or write mode and appear at
  `/mnt/<name>` for both file tools and shell commands — via the folder
  button, or just drag a folder from your file manager onto the
  composer. Git folders show
  their checked-out branch on the pill (live — it tracks checkouts made
  outside Loom).
- **The library itself**: drag the book icon from the top bar into the
  message area to attach the whole library — read-only by default, and
  flippable to write when the model should work on its own prompts and
  knowledge (see [curating knowledge](curating-knowledge.md)).

Chats are stored as plain JSON under `internals/chats/` in the library;
their artifacts live beside them and are deleted with them.
