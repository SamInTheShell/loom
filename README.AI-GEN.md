# Loom

A library-centric local LLM workbench: manage `llama-server` instances
(local or over SSH), keep prompts and a markdown knowledge base in a
folder you own, and chat with tools — shell commands sandboxed in
containers. CodeTree, distilled to its simplest form.

## Running

    uv run loom                # detaches from the terminal; logs → ~/.loom/loom.log
    uv run loom --foreground   # stay attached to the terminal
    uv run loom --debug        # foreground + DevTools open on launch

Launched from a terminal, Loom detaches (fork + setsid) so closing the
terminal never kills it. One instance at a time — a second launch points
you at the tray. The window's X closes to the system tray (servers keep
running); quit from the tray menu, which also manages servers. Ctrl+C in
--foreground: once asks in the window, twice within 3s force-quits.

`Ctrl+Shift+I` opens the real, fully-local Chromium DevTools any time.

## The library

Everything lives in a **library** — an ordinary folder you pick (or
create) on the first screen:

    <library>/
      loom.yaml        configuration (loom.yml works too)
      prompts/         all prompts: system, compaction, ...
      knowledge/       plain-markdown knowledge, searchable by the model
      containers/      container build files for the shell sandbox
      chats/           chat transcripts (closing a chat tab archives it)

The first screen lists recently used libraries (right-click an entry to
remove it, or omit it permanently; the list is clearable).

## loom.yaml

```yaml
models:
- name: Qwen 3.8 27B 128k
  ssh: ""            # set to an ssh destination for remote hardware
  context: 128000
  model: ~/.lmstudio/models/lmstudio-community/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q4_K_M.gguf
  mmproj: ~/.lmstudio/models/lmstudio-community/Qwen3.8-27B-GGUF/mmproj-Qwen3.8-27B-BF16.gguf
  flags: |
    -ngl 99
    -fa on
    -kvu
    -ctk q4_0 -ctv q4_0
    --spec-type draft-mtp
    --spec-draft-n-max 2
    --spec-draft-n-min 0
    --spec-draft-p-min 0.75
    -np 1

chat:
  permission_mode: always-ask   # default mode for new chats

# Permission MODES — pick per chat in the composer's shield pill.
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

Loom composes `llama-server --host <unix socket> -m … [--mmproj …]
--alias <name> -c <context> <your flags> --jinja` — the flags block is
passed verbatim.

## Design notes

* **No web listeners.** The UI loads over `file://` into pywebview
  (Qt/Chromium); managed servers bind **unix sockets**, reached locally
  via AF_UNIX and remotely over one multiplexed ssh connection.
* **Servers die with Loom.** Each server is a child of a bash supervisor
  held open by a pipe lifeline — the kernel closes it when Loom dies,
  however it dies.
* **Shell commands run in containers** as an unprivileged user (uid 1000),
  with `--network=none`; attached folders mount at `/mnt/<name>` read-only
  (view) or read-write (write).
* The markdown editor is a decorated **source** view (markers stay
  visible, styling happens around them) lifted from the databox personal
  cloud project; yaml and other code files get whole-file syntax
  highlighting in the same surface.

## Tests

    make test
