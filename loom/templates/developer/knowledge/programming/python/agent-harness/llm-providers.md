# LLM providers — instances, probing, and streaming adapters

The provider layer lets the agent loop (agent-loop.md) talk to any LLM
server — cloud APIs and local servers alike — through one normalized
interface. Two design decisions carry everything: providers are
USER-ADDED INSTANCES, not a fixed list (two Anthropic accounts, two
local servers on different machines — all coexist), and the whole
stack runs on stdlib `urllib`/`http.client` — no SDKs, no requests
library, no dependency on any vendor's client staying current. Three
wire adapters (OpenAI-compatible, Anthropic, Gemini) hide behind one
turn function signature; everything above them is provider-agnostic.

## Instances, not fixed ids

A TYPE contributes the wire protocol ("kind") and defaults; an
INSTANCE is one account/server of that type with its own name, base
URL, key, and optional SSH tunnel host (http-over-ssh.md):

```python
TYPES = [  # {type, kind, default baseUrl, needsKey}
  ("ollama",    "ollama",        "http://127.0.0.1:11434",   False),
  ("lmstudio",  "openai-compat", "http://127.0.0.1:1234/v1", False),
  ("openai",    "openai-compat", "https://api.openai.com/v1", True),
  ("anthropic", "anthropic",     "https://api.anthropic.com", True),
  ("gemini",    "gemini",
   "https://generativelanguage.googleapis.com",               True),
]
```

Instance records live in the app config as a list (`providerList`):
`{id: "prov-"+uuid8, type, name, baseUrl, apiKey, sshHost, enabled,
lastProbe}`. Chats reference the instance id, so renames and URL edits
never orphan a conversation. Update semantics for secrets: `None` =
keep existing, `""` = clear, anything else = replace — the UI can
edit a provider without ever round-tripping the key. The frontend
NEVER receives the key itself, only `hasKey` and a hint
(`"…" + key[-4:]` when the key is ≥ 6 chars).

The "ollama" kind exists only for probing: at chat time an ollama
base URL gets `/v1` appended and the instance is driven as
openai-compat (ollama's `/v1/chat/completions` supports tools).

## Probing — status only with evidence

Never assert "connected" from config. A provider is connected only
when a probe actually reached it, and the result records WHEN:
`{status: "connected"|"error", detail, models, probedAt}` (epoch ms),
cached in config so model pickers work across launches. A failed
probe keeps the previous model list — stale models beat an empty
picker. Timeouts are short (~6 s) with NO retries: the user is
watching; failing fast with the real error is the feature.

Per kind, one probe = connection test + model list + context sizes:

- ollama: `GET /api/version` + `GET /api/tags`; per model, context
  length hides in `POST /api/show` → `model_info` under the key
  ending `.context_length` (the prefix is the architecture name).
- openai-compat: `GET /models` (Bearer auth); `context_length` or
  `max_context_length` when present. LM Studio's OpenAI endpoint
  omits sizes but its native `GET /api/v0/models` (strip the `/v1`
  from the base) reports `max_context_length` — best-effort merge,
  silently skipped elsewhere.
- anthropic: `GET /v1/models` with `x-api-key` and
  `anthropic-version: 2023-06-01`; no context sizes in the API.
- gemini: `GET /v1beta/models?key=…&pageSize=100`; keep only models
  whose `supportedGenerationMethods` contains `generateContent`;
  strip the `models/` name prefix; `inputTokenLimit` is the context.

Map errors to actionable text: 401/403 → "check the API key"; a
URLError should NAME the host ("Connection refused — 127.0.0.1:1234"
reads as "nothing listens there"); a non-JSON 200 → "wrong base
URL?". These strings surface verbatim in the UI.

## The streaming endpoints

- openai-compat: `POST {base}/chat/completions`, `stream: true`,
  auth `Authorization: Bearer <key>`.
- anthropic: `POST {base}/v1/messages`, `stream: true`, headers
  `x-api-key` + `anthropic-version: 2023-06-01`.
- gemini: `POST
  {base}/v1beta/models/{m}:streamGenerateContent?alt=sse&key={k}`
  (key rides the query string).

All three speak SSE once `alt=sse` forces Gemini's hand. One opener
serves them all: build the request with `urllib.request` (or route
through the SSH tunnel — both return the same response class), map
HTTPError to `"HTTP {code}: {body[:400]}"` and URLError to
`"unreachable: {reason} — {netloc}"`. The response's read timeout
must be GENEROUS for streams (~600 s): a local model can spend
minutes prompt-processing before the first byte — that is work, not
a hang. `urlopen`'s timeout covers the header wait AND every read.

## The SSE loop

```python
def sse_events(resp, cancel):
    register_stream(cancel, resp)   # stop() closes resp → read unblocks NOW
    last_data = time.monotonic()
    try:
        for raw in resp:
            if cancel.is_set():
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                if line and time.monotonic() - last_data > STALL_TIMEOUT:
                    raise ChatError("stream stalled — keepalives but no data")
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            last_data = time.monotonic()
            yield obj
    finally:
        unregister_stream(cancel); resp.close()
```

Three ways a stream dies, three distinct handlers: `TimeoutError`
from the read = server gone (turn it into a clear "sent nothing for
N minutes" error); non-data lines flowing but no `data:` event for
~180 s = keepalive-only stall (a server can look "alive" forever
while doing nothing — without this guard the UI shows "thinking"
indefinitely); Stop = a cancel Event PLUS closing the registered
response, because an Event cannot interrupt a read blocked on a
socket (streaming-cursor.md consumes the resulting event stream).

## Adapter: OpenAI-compatible

Tool-call deltas arrive FRAGMENTED: each chunk's
`choices[].delta.tool_calls[]` entries carry an `index`, maybe an
`id` and `function.name` (first fragment), and a piece of
`function.arguments` — a JSON STRING that must be concatenated
across deltas and parsed only at the end:

```python
calls = {}                                  # index -> accumulating slot
for tc in delta.get("tool_calls") or []:
    slot = calls.setdefault(tc.get("index", 0),
                            {"id": "", "name": "", "args": ""})
    if tc.get("id"):                slot["id"] = tc["id"]
    fn = tc.get("function") or {}
    if fn.get("name"):              slot["name"] = fn["name"]
    if fn.get("arguments"):         slot["args"] += fn["arguments"]
```

`delta.content` is streamed text; `delta.reasoning_content` (or
`reasoning` on some servers) is thinking — render de-emphasized,
never send back. Usage: request it with
`stream_options: {"include_usage": true}` — OpenAI proper won't send
a usage chunk unrequested. Some compatible servers 400 on the
option: on an HTTP 400 with it set, remember the base URL for the
process lifetime, drop the option, retry once. The usage object
rides the final chunk (some servers repeat it — keep the latest):
`prompt_tokens`, `completion_tokens`,
`prompt_tokens_details.cached_tokens`,
`completion_tokens_details.reasoning_tokens`. Reasoning control is
`reasoning_effort` in the body. Omit the `tools` key entirely when
empty — some servers 400 on an empty array.

## Adapter: Anthropic

`max_tokens` is REQUIRED. The stream is typed events, and content is
a sequence of BLOCKS opened/streamed/closed by
`content_block_start` / `content_block_delta` / `content_block_stop`;
track the current block and dispatch on delta type: `text_delta`
(visible text), `input_json_delta` (`partial_json` fragments of a
tool call's arguments — concatenate, parse at block stop),
`thinking_delta` + `signature_delta` (extended thinking), plus
`redacted_thinking` blocks that arrive whole. `tool_use` block
starts carry `id` and `name` up front. An `error` event carries
`error.message` — raise it.

Usage is split: `message_start` carries the prompt-side counts,
`message_delta` the cumulative output count (it supersedes the
partial figure in message_start) — merge keys as they arrive.
`input_tokens` EXCLUDES cache traffic: the true prompt footprint is
`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.
Extended thinking is opt-in via
`thinking: {type: "enabled", budget_tokens: N}` and `max_tokens`
must EXCEED the budget. Hard-won: with thinking + tools, the API
demands the thinking blocks (with signatures) back VERBATIM in the
assistant turn — keep them in the in-flight history even if the
persisted transcript stays thinking-free.

## Adapter: Gemini

The system prompt goes in `systemInstruction.parts[].text`, history
in `contents` with roles `user`/`model`. Chunks carry
`candidates[].content.parts[]`: a part with `thought: true` is
thinking; `text` is visible; `functionCall {name, args}` arrives
WHOLE (args already an object — no delta accumulation) but with no
id, so synthesize one (`"call_" + uuid`). `usageMetadata` is
CUMULATIVE on every chunk — latest wins: `promptTokenCount`,
`candidatesTokenCount` (add `thoughtsTokenCount` for true output),
`cachedContentTokenCount`. Thinking control is
`generationConfig.thinkingConfig: {thinkingBudget, includeThoughts}`
(budget 0 disables where the model allows).

## Tool results back — per API

- openai-compat: `{"role": "tool", "tool_call_id": id, "content":
  text}`. Images can't ride the tool channel — append a follow-up
  user message with an `image_url` data URI (and transcode webp/gif
  to PNG first; many compat servers 400 on other data URIs).
- anthropic: a USER message containing
  `{"type": "tool_result", "tool_use_id": id, "content": …,
  "is_error": bool}`; image blocks are allowed inside the content.
- gemini: a user message with `parts: [{"functionResponse": {"name",
  "response": {"result": text}}}]`; images again as a follow-up
  user message with `inline_data`.

Cap result text as a BACKSTOP only (~256 KB): each tool should bound
its own output with paging hints; a low flat cap here would eat the
hint along with the content.

## The normalized layer

Each adapter is one function with the same contract:

```python
text, tool_calls, assistant_msg = turn(cfg, model, history,
                                       cancel, push, tools, ...)
```

- `text` — the visible completion.
- `tool_calls` — `[{id, name, args}]` with `args` a parsed dict
  (unparseable argument JSON → `{}`; never crash the loop on a
  model's malformed output).
- `assistant_msg` — the PROVIDER-NATIVE message to append to that
  provider's history verbatim (OpenAI `tool_calls` with the raw
  arguments string; Anthropic content blocks including signed
  thinking; Gemini `parts`). History stays provider-native per run;
  only the persisted transcript is normalized.

`push` emits normalized events the UI and agent loop consume:
`{kind: "delta"|"thought"}` for streamed text, `turn_start` when the
request hits the wire (the UI measures time-to-first-token from it),
and one `usage` event per turn:
`{inTotal, out, cacheRead, cacheWrite, reasoning}` where `inTotal`
is the FULL prompt-side footprint (cache included) — the number that
actually occupies the context window. Anchor context meters to these
real counts; a chars/4 heuristic drifts unboundedly.

Transient errors (HTTP 408/409/429/5xx, connection reset, stall)
warrant ONE retry per turn after a short backoff — history already
holds every completed tool result, so re-asking loses nothing. 4xx
auth/bad-request errors are not transient; retrying re-fails
identically. Virtual providers (virtual-providers.md) layer routing
and failover ABOVE this: a chat may name a routing bundle instead of
an instance, resolved per send.

## Rules

- Status needs evidence: "connected" only after a probe reached the
  server, always with a timestamp; cache the last real observation,
  never fabricate one.
- Keys never cross to the frontend — `hasKey` + last-4 hint only.
- Accumulate OpenAI tool arguments as STRINGS keyed by delta index;
  parse once at end of turn. Anthropic: concatenate `partial_json`
  per block. Gemini: args arrive whole; synthesize call ids.
- Omit empty `tools` arrays; some servers reject them.
- Streaming read timeout must be minutes, not seconds — local
  models legitimately send nothing while prompt-processing. Pair it
  with a keepalive-stall guard, or dead servers look like thinking.
- Stop = cancel Event + close the live response; a blocked socket
  read ignores Events.
- Feed each provider its OWN history dialect; normalize only at the
  event/persistence boundary. Echo Anthropic thinking blocks back
  verbatim when tools are in play.
- Retry once on transient errors only; surface everything else with
  the provider's real words.
