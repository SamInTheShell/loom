# Loom

A library-centric local LLM workbench: point it at inference APIs you
run yourself (llama.cpp's `llama-server`, or `ninfer` - locally or over
SSH), keep prompts and a markdown knowledge base in a folder you own,
and chat with tools - shell commands sandboxed in containers. Loom does
NOT launch or manage inference processes.

## Running

    uv run loom                # detaches from the terminal; logs → ~/.loom/loom.log
    uv run loom --foreground   # stay attached to the terminal
    uv run loom --debug        # foreground + DevTools open on launch

Launched from a terminal, Loom detaches (fork + setsid) so closing the
terminal never kills it. One instance at a time - a second launch points
you at the tray. The window's X closes to the system tray (chats and
terminals keep running); quit from the tray menu. Ctrl+C in
--foreground: once asks in the window, twice within 3s force-quits.

`Ctrl+Shift+I` opens the real, fully-local Chromium DevTools any time.

## The library

Everything lives in a **library** - an ordinary folder you pick (or
create) on the first screen:

    <library>/
      loom.yaml        configuration (loom.yml works too)
      prompts/         all prompts: system, compaction, ...
      knowledge/       plain-markdown knowledge, searchable by the model
      containers/      container build files for the shell sandbox
      internals/       chat transcripts, artifacts

The first screen lists recently used libraries (right-click an entry to
remove it, or omit it permanently; the list is clearable).

## loom.yaml

```yaml
providers:                      # the inference APIs Loom talks to
- name: workstation
  type: llama-cpp               # llama-cpp | ninfer
  url: http://127.0.0.1:8080
  ssh: ""                       # optional: resolve the url FROM this ssh
                                # host over an stdio tunnel (KEYS ONLY -
                                # password prompts are refused)

chat:
  provider: workstation         # default provider for new chats
  model: ""                     # default model id; empty = first listed
  permission_mode: always-ask   # default mode for new chats

# Permission MODES - pick per chat in the composer's shield pill.
# Built-ins: always-ask, allow-edits, always-allow. Override per tool or
# define new modes. Levels: allow | ask | deny | disabled.
# Tools: knowledge_search, read_file, list_dir, grep, find_files,
#        write_file, edit_file, shell
permission-modes:
  always-ask:
    tools: {}
  read-only:            # example custom mode
    tools:
      write_file: disabled
      edit_file: disabled
      shell: disabled

containers:          # shell commands run here, never on the host
  engine: auto       # auto | podman | docker
  default: sandbox
  definitions:
  - name: sandbox
    file: containers/sandbox.Containerfile
```

Models are pulled live from each provider's API (`GET /v1/models`),
with context windows from the provider's own metadata. Requests are
plain OpenAI-style `/v1/chat/completions` streams with the llama.cpp
extensions (`timings_per_token`, `return_progress`,
`stream_options.include_usage`) so live tok/s, prefill progress, and
real usage are always available.

## Design notes

* **Loom launches no inference.** Providers are other people's
  processes reached over HTTP; quitting Loom leaves them untouched.
* **No web listeners.** The UI loads over `file://` into pywebview
  (Qt/Chromium). Remote providers are reached over one multiplexed ssh
  connection carrying per-request stdio tunnels - no local ports.
  BatchMode ssh: key auth only, password prompts fail fast.
* **Shell commands run in containers** as an unprivileged user (uid 1000);
  attached folders mount at `/mnt/<name>` read-only (view) or read-write
  (write). Networking has three per-chat/per-terminal modes: `none`
  (`--network=none`), `loopback` (host `127.0.0.1` services at 10.0.2.2
  via slirp4netns `allow_host_loopback` + `outbound_addr=127.0.0.1`,
  podman only), and `on`. The model is told the active mode in its tool
  spec, system prompt, and on any connectivity-looking failure.
* **Diagnostics are first-class**: per-turn stats rows (tok/s, prefill
  speed, TTFT, cache hits), a live tok/s + prompt-progress readout
  while streaming, and a per-chat Diagnostics view (summary tiles +
  correlated graph/table) reachable from the context chip. It refreshes
  live as the chat runs, and can pop out into its own OS window
  (frontend/diagwin.html, sharing the bridge and event bus); child
  windows always close with the main window.
* The markdown editor is a decorated **source** view (markers stay
  visible, styling happens around them) lifted from the databox personal
  cloud project; yaml and other code files get whole-file syntax
  highlighting in the same surface.

## Tests

    make test
