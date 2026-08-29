# Inference tips

Practical llama-server knowledge for the `flags:` block. All of it is
ordinary llama.cpp — anything its docs cover works here.

## The flags that matter first

- `-ngl 99` — offload all layers to the GPU. If you run out of VRAM,
  lower it (layers spill to CPU) or pick a smaller quant.
- `-fa on` — flash attention; faster and leaner, keep it on when your
  build supports it.
- `-c <tokens>` — Loom sets this from `context:`. Bigger windows cost
  VRAM in the KV cache; see below.
- `-np 1` — one parallel sequence. The context splits across `-np`, so
  keep it 1 unless you deliberately serve concurrent requests.

## Context vs memory

The KV cache dominates memory at large contexts. Quantizing it helps a
lot with minor quality cost:

```
-ctk q4_0 -ctv q4_0     # quantized KV cache (needs -fa on)
-kvu                    # unified KV — one shared buffer, less waste
```

Rule of thumb: if a 128k context OOMs, try KV quantization before
shrinking the window; if it still OOMs, halve `context:`.

## Model quants

`Q4_K_M` is the sweet spot for most models — noticeably smaller than Q6
with little practical loss. Go `Q5_K_M`/`Q6_K` when VRAM allows, `IQ4`/
`Q3` variants only when it doesn't. A bigger model at Q4 usually beats a
smaller one at Q8.

## Vision (mmproj)

Multimodal models ship a second GGUF — the projector, named `mmproj-…`.
Set it as `mmproj:` in the model entry (the Models scanner pairs them
automatically) and image attachments in chats start working. Both files
must come from the same model family.

## Speculative decoding

Some models ship draft/MTP support for a large speedup at identical
output quality:

```
--spec-type draft-mtp
--spec-draft-n-max 2
--spec-draft-n-min 0
--spec-draft-p-min 0.75
```

Watch the tok/s in the chat stats row with and without. **Only enable
this for models that actually ship MTP/draft layers** — on any other
model the server refuses to START, with this signature in the log:

```
llama_init_from_model: context type MTP requested but model doesn't contain MTP layers
srv  load_model: failed to create MTP context
```

The fix is always the same: delete (or re-comment) the `--spec-*` lines
in that model's `flags:` block.

## Reasoning effort

Thinking models trade latency for depth, and each family switches
differently. You don't hand-edit requests: the **brain** button on the
model's row in a chat's model menu picks the method and level, per
model, persisted for the library.

- `reasoning_effort` — the request field, forwarded by llama-server into
  the chat template. Graded families take levels (Qwen 3.8:
  off/low/medium/xhigh — other names error at request time; gpt-oss:
  low/medium/high). "off" sends `none`, which disables reasoning.
- `enable_thinking` — the boolean template kwarg (Qwen3-era on/off).
- `/think` — the prompt-suffix soft switch for models trained on it.

**Default** sends nothing and lets the server/template decide — always
one click away if a model misbehaves.

## Tool calling

Loom appends `--jinja` automatically — tool calling *requires* the jinja
chat-template engine; without it a model silently never calls tools.
Pick instruct models whose template defines tools (Qwen, Llama,
Mistral families all do).

## Embedding servers

A model entry whose flags include `--embedding` serves embeddings
instead of chat; Loom omits `--jinja` for those (no chat template to
render).

## When something is slow or wrong

1. Servers tab → **Log**. llama-server prints layer offload counts,
   KV-cache sizes, and template warnings at startup — most mysteries are
   answered there.
2. Check the chat stats row: low tok/s with high prompt time usually
   means CPU spill (`-ngl` too low or VRAM exhausted).
3. One change at a time; flags interact.
