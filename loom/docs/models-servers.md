# Providers and models

Loom does **not** launch inference. A **provider** in `loom.yaml` is
either a llama.cpp `llama-server` you run yourself (a modern build -
Loom reads the served context from `meta.n_ctx` on `/v1/models`), or a
**hosted vendor**: OpenAI, Anthropic, Google Gemini, Google Vertex AI,
or Amazon Bedrock. Gemini, Vertex and Bedrock are reached through
their OpenAI-compatible endpoints; Anthropic gets a dedicated
`/v1/messages` adapter. Hosted-vendor support is **new and untested
against the live APIs** - please report anything that misbehaves so we
can improve it. Models come from each provider's own listing where the
vendor has one (Vertex doesn't - you type the model id).

## loom.yaml provider entries

```yaml
providers:
- name: workstation           # how chats refer to it
  vendor: llama-cpp           # llama-cpp | openai | anthropic |
                              # gemini | vertex | bedrock
  url: http://127.0.0.1:8080  # base URL (hosted vendors have a
                              # default; Vertex AI needs yours)
  ssh: ""                     # optional ssh destination (see below)
- name: claude
  vendor: anthropic           # API key via the provider card's Key
                              # button - the OS keyring, never here

chat:
  provider: workstation       # default provider for new chats
  model: ""                   # default model id; empty = first listed
```

Start `llama-server` however you like (`llama-server -m model.gguf
--port 8080 --jinja …`) - see [inference tips](inference-tips.md) for
flags worth knowing. Tool calling needs `--jinja` on llama-server.

**Truncated long replies?** Some servers cap output per request unless
the client asks for more (ninfer-style servers default to 8192 tokens),
which cuts big replies - and big tool calls - mid-stream. Set
`chat.max_output` in loom.yaml (or the Chat defaults dialog) and Loom
sends it as `max_tokens` on every turn; 0 leaves the server's own
default in charge. When a cut still happens: a truncated *answer* is
marked stopped (Continue resumes it), and a truncated or unparseable
*tool call* is **discarded and replaced with a failed tool result**
naming the limit that caused it - the loop keeps going and the model
retries with smaller pieces. Three broken calls in a row stop the loop
honestly. Relatedly, `chat.read_gate` (default 32768 tokens) keeps the
model from reading huge files whole - `read_file` refuses past the
gate and the model reads offset/limit slices instead; 0 disables it.
Hosted vendors speak tools out of the box; note that they don't report
a context window, so the ctx chip shows an estimate against an unknown
limit, and live tok/s + prompt-progress (llama.cpp extensions) don't
apply.

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
(key included) before anything is written. Each card also carries
**Edit…** (rewrites just that entry - a rename moves its keyring key
along) and **Remove** (drops the entry and its key). Every write is
validated first; the rest of the file, comments included, stays
byte-for-byte. For the whole file at once, **Edit loom.yaml** opens
the Configuration tab's validated editor.

Providers are other people's processes - Loom never starts, stops, or
supervises them, and quitting Loom leaves them exactly as they were.

## Serving it all back out - the API Server tab

The **API Server** tab (globe) aggregates every provider's models into
one OpenAI-compatible API (`/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/models`) for other tools on your machine or LAN.
Interface and port persist in `loom.yaml`'s `api:` section; the on/off
toggle deliberately does not - every Loom launch starts with it OFF.
An optional key (stored in the OS keyring) gates every route except
`/health`.

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
