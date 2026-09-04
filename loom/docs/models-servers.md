# Providers and models

Loom does **not** launch inference. You run an inference server
yourself - llama.cpp's `llama-server` or `ninfer-serve` - wherever the
hardware lives, and Loom talks to its HTTP API. Each such endpoint is a
**provider** in `loom.yaml`; the models come from the provider's own
API (`GET /v1/models`), so there is nothing to configure about model
files, flags, or process management here.

## loom.yaml provider entries

```yaml
providers:
- name: workstation           # how chats refer to it
  type: llama-cpp             # llama-cpp | ninfer
  url: http://127.0.0.1:8080  # the API's base URL
  ssh: ""                     # optional ssh destination (see below)

chat:
  provider: workstation       # default provider for new chats
  model: ""                   # default model id; empty = first listed
```

Start `llama-server` however you like (`llama-server -m model.gguf
--port 8080 --jinja …`) - see [inference tips](inference-tips.md) for
flags worth knowing. Tool calling needs `--jinja` on llama-server;
ninfer speaks tools out of the box.

## Over SSH

Set `ssh:` to a destination (`user@host`, or a `~/.ssh/config` alias -
ProxyJump and friends apply) and the `url` is resolved **from that
host**: `http://127.0.0.1:8080` then means "port 8080 on the remote
machine". Traffic rides an ssh stdio tunnel over one multiplexed
connection; no local port is ever opened.

**Key authentication only.** Loom runs ssh with `BatchMode=yes`: a host
that would ask for a password (or an unloaded key passphrase) fails
immediately with `Permission denied` instead of hanging on a prompt.
Load the key into `ssh-agent` (`ssh-add`) or use an unencrypted key
file.

## The Providers tab (Ctrl+E)

One card per provider: reachability, the models it lists (with each
model's context window), a **Probe** button to re-check, and a **Key**
button for servers started with `--api-key` - the key is stored in this
machine's OS keyring (never in `loom.yaml`) and rides every request as
`Authorization: Bearer`. **Add provider…** writes a new entry into
`loom.yaml` non-destructively, with a **Test** button that probes
(key included) before anything is written.

Providers are other people's processes - Loom never starts, stops, or
supervises them, and quitting Loom leaves them exactly as they were.

## Picking models in a chat

The composer's model button (Ctrl+.) opens the picker: **provider →
model → reasoning**, all keyboard-driven - ↑/↓ walk (starting from the
current value), → descends, ← goes back, Enter picks, 1-9 jump. Each
model row shows its context window and carries two per-model controls:
the **pin** floats it to the top of the list, and the **brain** opens
its reasoning submenu - first *how* to steer thinking
(`reasoning_effort` request field, `enable_thinking` template kwarg, or
a `/think` prompt switch), then the level. The configured level shows
on the composer's model button. Both persist per library.
