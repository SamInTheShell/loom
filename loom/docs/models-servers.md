# Models and servers

## The Models utility (⤓ in the top bar)

- **Fetch** a GGUF by URL — or give it a Hugging Face repo and pick the
  quant — into `~/.loom/models/`. When the repo ships an `mmproj`
  (the vision projector), it downloads automatically alongside the
  quant you pick.
- **One tab per machine**: Local first, then every ssh host you add
  (**+ add ssh host…**). Drag a host tab to reorder; right-click it to
  rescan or forget. Each tab scans its machine's usual model folders on
  first open — `~/.loom/models`, LM Studio's folders, the Hugging Face
  cache, `~/models`, `~/Downloads` — with a reload button and a filter.
  `mmproj` files are flagged and paired with the model beside them.
- **Copy to…** on a Local row pushes that model (its mmproj comes
  along) to a host's `~/.loom/models/` over ssh.
- **Server…** on any row opens the New-model wizard with that machine
  and file already chosen.

Turning a file into configuration is the **New model… wizard** — the
Server… button here, the Servers tab, and a chat's model menu when no
models exist: pick the host and GGUF (choosing it advances), shape
name / context / backend, review the exact yaml, and it lands
non-destructively at the end of the `models:` block.

## loom.yaml model entries

```yaml
models:
- name: Qwen 27B          # the name chats select; also the --alias
  ssh: ""                 # ssh destination = run on that machine
  context: 128000         # -c
  model: ~/models/qwen.gguf
  mmproj: ~/models/mmproj-qwen.gguf   # optional, enables images
  binary: llama-server    # optional, a specific build/path on that host
  flags: |                # passed to llama-server verbatim
    -ngl 99
    -fa on
```

Loom composes the socket, `-m`, `--mmproj`, `--alias`, `-c`, and
`--jinja` (required for tool calling); every other flag is yours,
verbatim. `~` expands on the machine that runs the server.

## The Servers tab

Start / stop / restart each model; the dot tracks stopped → loading →
running, and the **Log** button shows llama-server's real output (the
honest answer when something fails). Edits to loom.yaml apply on the
next restart.

Servers bind **unix sockets only** — no TCP ports on any interface.
Remote servers are reached over one multiplexed ssh connection
(`~/.ssh/config` aliases, keys, ProxyJump all apply; passphrase prompts
surface in-app). Every server is supervised so it dies with Loom —
however Loom dies — and never orphans.

The model popup in a chat's input panel can also start/stop servers,
filter by name, and shows live state dots. Two per-model controls live
on its rows: the **pin** floats a model to the top of every picker
(handy past a handful of entries), and the **brain** opens that model's
reasoning-effort submenu — the configured level shows under the model
name and on the composer's model button. Both persist per library.
