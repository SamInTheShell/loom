# Chats

A chat streams against one of your configured providers (a
llama-server - through an ssh tunnel when the provider has an `ssh:`
destination - or a hosted vendor API). The message area follows llama.cpp's web chat:
your messages in bubbles (nudged right), the model's as floating
markdown (nudged left), tool activity as collapsible cards between
them, and a stats row (tok/s, prefill speed, time-to-first-token,
token counts with cache reuse) after each turn. While a reply streams,
a live tok/s reading rides after the cursor, and while the server
reads your prompt a real progress readout (percent, tokens, ETA)
replaces the guessing.

## Sending

- **Enter** sends, **Shift+Enter** breaks the line. **Up/Down** at the
  start of the input walk your message history; **PgUp/PgDn** scroll the
  thread without leaving the input; **Esc** stops a streaming reply.
  **Ctrl+I** focuses the input from anywhere; a waiting permission card
  answers to **Ctrl+Enter** (allow) / **Ctrl+Esc** (deny). Hold **Ctrl**
  to reveal every shortcut in place.
- A sent message moves to the **queue panel** above the input and
  dispatches when the chat is idle. Queue rows can be edited back into
  the input, force-sent, or cancelled. Sending while a reply streams
  just queues the next message. An unreachable provider bounces the
  send with the honest error - the message stays queued.
- **Retry / Continue** appears under the thread when the last word was
  yours or a reply stopped early - both resume the loop in place.
  **Ctrl+R** resumes however the chat ended: it triggers the banner when
  one is showing, and on a *finished* reply it asks the model to simply
  keep going with no new input. On an **empty** chat it makes the model
  produce the first message - the conversation opens from the system
  prompt alone.
- **Ctrl+Shift+R** toggles **auto-continue**: after every completed
  response the chat continues itself, no new input needed. A pulsing
  bar above the composer is the signal while it's armed - click its
  button (or Ctrl+Shift+R again) to stop. It disarms itself if you stop
  generation, the chat errors, or the model runs out of things to say;
  messages you queue while it's armed still go out first.
- **Auto-scroll is deterministic.** The thread follows the stream only
  while you're "following", and only explicit actions change that:
  scrolling away from the bottom holds your spot; scrolling back down,
  the ⬇ jump button, or SENDING a message re-arms following. While you
  hold a spot, nothing moves it - redraws put the entry you were
  reading back exactly where it was, collapsing a thought above your
  view compensates for the height change, and streaming just grows
  below you.
- **Following reads from the start.** A follower's view advances only
  until the newest reply's beginning reaches the top of the panel, then
  holds - come back to a long reply and it's pinned at its first line,
  readable top to bottom, while the rest streams in below the fold (the
  ⬇ button marks the extra). Scrolling to the very bottom or clicking ⬇
  switches to tail-following for that reply; the next reply re-clamps
  at its own start.
- Closing a chat tab archives it (reopen from the Chat Archive tab); a
  streaming chat asks before cancelling. **Ctrl+N** opens a plain new
  chat from anywhere; **Ctrl+Shift+N** (from a chat tab) clones the current
  chat's setup - model, permission mode, attachments - into a fresh
  context window.

## The tools bar - manipulating signals

The strip across the top of every chat holds tools that manipulate the
SIGNALS the model receives (more will land here over time).

The **knowledge chip** cuts the knowledge base out of a chat entirely:
the knowledge_search tool is not offered, /knowledge is refused by file
tools and not mounted in shells, and the system prompt stops describing
- or even mentioning - the base. Like every cut here it's per chat,
persists, and rides into forks.

**Network** (Ctrl+/) opens its three modes as a panel - no network /
loopback only / network on. **MCP tools** overrides any running MCP
tool's permission for THIS chat (allow / ask / deny / disabled);
an override beats loom.yaml's per-mode entries and the MCP tab's
defaults, and "default" hands the decision back. **Artifacts**
(Ctrl+[) holds both the on/off cut and every file the model delivered
- open, save, or dismiss them from the panel (the timeline entries in
the chat keep their own buttons either way). **Env signals** (shown
when an environment is loaded) exposes or hides each variable's NAME
per chat - hidden variables still load into shell containers; the
model just isn't told they exist.

**Terminal** (right side of the bar) opens a separate window with a
real shell in this chat's EXACT container setup - the same image, the
same /mnt folder mounts, /knowledge (respecting its cut) and
/uploads, the chat's own /home/loom, the same network mode and
environment. Use it to inspect the environment the way the model sees
it: what's on disk, what the network reaches, which variables are set
(env signals hidden from the model still load - the terminal shows the
truth). The shell is a live mirror: change the chat's container,
mounts, network, environment, or the knowledge cut and it
restarts itself to match, keeping its scrollback. It shares the chat's
home but not its processes - the model's shell commands still run in
their own fresh containers. Closing the window kills the shell.

Both sides of the conversation carry time signals on the wire: every
user message begins with its UTC send time in [brackets], and every
assistant reply with the time it was generated. The assistant's can be
switched off with `chat.assistant_signals: false` in loom.yaml (also a
checkbox in the Configuration tab's Chat defaults dialog). Signals live
on the wire, never in the visible message - a model that imitates its
own stamped history has the echoed stamp stripped from its reply - but
nothing about them is hidden from you: hover the speaker's name ("you",
or the agent's) to see exactly what that message reports to the model,
including, under time travel, the real time next to the signalled one.
The agent's hover also names the model behind it, since the header
shows the agent's NAME - `chat.assistant_name` in loom.yaml, "loom" by
default.

**Time travel** shifts the time signal: normally every message carries
its real UTC time in [brackets] on the wire, and with an offset armed
each *new* message reports real-time + offset instead - forward or
backward - to probe the model's signal awareness. The **time travel
button** opens its panel: drag the slider (fine steps near the middle,
years at the ends), or double-click the value and type an offset like
`+1y 2d 5m 3s` (units `y w d h m s`, sign first). While armed the
button shows the offset and **×** clears it. The panel's **datetime
signals toggle** goes further: OFF hides every datetime from the model
for this chat - no session-start stamp, no message brackets on either
side. Real timestamps, the UI, and already-sent stamps are never
touched, and both settings survive restarts and ride along into forks.

## Copy, fork, delete - working the history

Hover any user message, model reply, thought entry, or tool card for
its action buttons (top right):

- **Copy** grabs that entry's text - the message, the thought, or the
  tool call with its result. (Code blocks inside rendered replies keep
  their own per-block Copy button.)
- **Fork** opens a NEW chat whose history is this one truncated right
  after that entry - same provider, model, permission mode, container,
  environment, network and attachments; everything after the fork point
  stays here. If tool calls ran after that point in the original chat,
  the fork carries an injected thought telling the model that files or
  other external state may have changed and to re-check before acting.
  Forking at one of your messages ends the new chat on it, so the Retry
  banner is ready to regenerate a different reply.
- **Delete** (right of fork, after a confirmation) removes the entry
  from the history - the model no longer sees it. Deleting a reply that
  called tools takes its results along; deleting a tool card takes that
  call out of the calling turn, so the conversation the model sees
  always stays well-formed. Deleting a **thought** removes just the
  thinking - the reply it belongs to stays, unless the thought was all
  there was of that turn. Not available while a reply is streaming.

## Tools and permissions

The model gets file and search tools over the knowledge base and
attached folders (`/mnt/<name>`), plus `shell`. What runs freely is the
**permission mode** - the shield pill top-right of the input panel:
`always-ask` (changes and shell confirm), `allow-edits` (edits and
sandboxed shell run without asking), `always-allow`, or your own defined
under `permission-modes:` in loom.yaml (levels per tool: allow / ask /
deny / disabled) - the Configuration tab's **Permission modes…** dialog
edits the whole thing as a tools × modes matrix. Switching the mode
also re-decides any tool call already waiting for permission.

Stdio **MCP servers** defined under `mcp-servers:` in loom.yaml add
their tools too, surfaced to the model as `mcp_<server>_<tool>` - the
MCP Servers tab defines, enables, and sets per-tool default permissions
(a `permission-modes:` entry with the full function name still wins per
mode).

The security boundary is the **container**, not the tool list:

- **Shell always runs in a container** - specifically the one named by
  `containers.default` in loom.yaml (terminals can pick per tab; chats
  use the default) - as an unprivileged user, never on the host. View-mode
  folders and the knowledge base (`/knowledge`) are mounted read-only
  at the kernel level, so the shell is safe read-only tooling; flip a
  pill to *write* deliberately when you want edits.
- **Network is off by default - three modes.** The tools bar's network
  panel picks per chat (Ctrl+/); nothing in a library
  file can change it:
  - **no network** - `--network=none`. The container still has its own
    private loopback, so an in-container dev server + curl works.
  - **loopback only** - the HOST's `127.0.0.1` services (your
    llama-server, a local database…) are reachable at `10.0.2.2`
    inside the container, and nothing else is. Implemented with
    podman's slirp4netns (`allow_host_loopback` + outbound sockets
    bound to the host loopback, which makes external routes
    kernel-unreachable - no firewall involved). Needs podman; docker
    has no rootless equivalent and refuses honestly.
  - **network on** - the engine's default network.

  The model is TOLD the mode in three places - the system prompt, the
  shell tool's own description, and a note appended to any failed
  command whose output looks like a connectivity error - so it asks you
  to change the chip instead of burning turns troubleshooting phantom
  network problems.
- **Every shell call carries a model-chosen timeout** (capped at an
  hour): a hung command kills itself instead of waiting for you to
  cancel it, and the result names the budget it hit so the model can
  size the next attempt properly.

Two pills left of the permission mode steer the shell's world:

- **Container** - which `containers:` definition runs shell commands;
  chats start on `containers.default`, switchable per chat.
- **Environment** - a named env-var set loaded into shell containers
  (chats start with none). Definitions live in the library's
  `environments.yaml` - plain values plus SECRET STUBS; secret values
  live in your OS keyring, set per machine in the Environments tab
  (reachable from the pill). With network on, this lets the model drive
  cloud CLIs with real credentials - the model is told which variables
  are set, never their values.

## Artifacts

Artifacts are **outbound deliverables**: files the model hands you
to download, nothing else. There is no artifacts folder in the
container and no `/artifacts` path in the file tools - the model's
workspace is its persistent `/home/loom` (and write-mode mounts), and
the ONLY way it can give you a file is the explicit
**deliver_artifact** tool. It cannot read or edit a deliverable back,
so artifacts can't be abused as scratch space.

Every delivery arrives two ways at once: in the tools bar's
**artifacts panel** (the button shows the count), and as a timestamped
**delivery entry in the chat itself**, so you can always see when a
file arrived and open or save it from the history.

The same panel's **on/off toggle** (Ctrl+[ opens it) disables delivery
per chat: the deliver_artifact tool is withdrawn (and refused if
called anyway) and no new deliveries land - already-delivered
artifacts stay viewable.

- **Click a pill (or a delivery entry's name)** to open the artifact in
  its own window: text opens in the real markdown/code editor and
  **saves back in place** (your copy only - the model cannot see
  artifacts once delivered), images render, and anything else offers a
  download button. The window also has **Save to…** for a copy, and closes with
  the app.
- **× on a pill clears it** from the attach bar; the file and the
  delivery entry stay, and the pill returns if the model regenerates
  the artifact.
- Files carry a **save** button, folders save as **zip** downloads,
  images get hover previews.

Your uploaded images are staged at `/uploads` (read-only) so shell
and file tools can process them. Artifacts persist with the chat and are
purged when it is permanently deleted from the Archive.

## Reasoning effort

The **brain** button on each row of the model menu configures how hard
that model thinks: pick the method (`reasoning_effort` request field -
graded models like Qwen 3.8 take off/low/medium/xhigh; the boolean
`enable_thinking` template kwarg; or the `/think` prompt switch) and
the level. The model button shows the configured level at a glance;
**Default** sends nothing and lets the server decide.

Read tools show numbered, syntax-highlighted previews; edits show real
diffs - in the permission card too, so you approve what will actually
change. Resolved cards collapse to one line; click to inspect.

## Context and compaction

The **context chip** (left of the network toggle) shows usage; hover for
the breakdown - system prompt, messages, thoughts, tool results and
definitions, estimated total vs the model's real window, and the last
turn's actual token count.

When usage crosses `chat.compaction.threshold` (default 80%), the
conversation is summarized with `prompts/compaction.md` and the summary
replaces the older turns on the wire - the full history stays in the
file and on screen (a collapsible "context compacted" card marks the
seam). "Compact now" lives in the chip's hover card. Older thinking is
dropped from the wire by default; only the latest turn's thoughts ride
along. That's the **thoughts chip**, per chat (Ctrl+]) - "latest
thought" vs "all thoughts" - and `chat.thought_truncation` in loom.yaml
only sets the default for NEW chats.

## Attachments

- **Images** (png/jpeg) attach via the image button - they reach the
  model when it runs with an `mmproj` projector (see
  [inference tips](inference-tips.md)).
- **Folders** attach in view (read-only) or write mode and appear at
  `/mnt/<name>` for both file tools and shell commands - via the folder
  button, or just drag a folder from your file manager onto the
  composer. Git folders show
  their checked-out branch on the pill (live - it tracks checkouts made
  outside Loom).
- **The library itself**: drag the book icon from the top bar into the
  message area to attach the whole library - read-only by default, and
  flippable to write when the model should work on its own prompts and
  knowledge (see [curating knowledge](curating-knowledge.md)).

Chats are stored as plain JSON under `internals/chats/` in the library;
their artifacts live beside them and are deleted with them.
