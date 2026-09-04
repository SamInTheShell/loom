# The agent loop - thread-per-message tool-calling with a UI gate

The core of an agentic workbench is one loop: stream a model turn,
collect the tool calls it made, gate each call through the user,
execute, feed the results back, repeat until the model stops calling
tools. Run it on ONE background daemon thread per in-flight message -
the UI bridge thread must return immediately (ui-bridge.md), and
threads give you blocking reads, blocking permission waits, and a
cancel Event, all with stdlib only. Provider request/stream mechanics
live in llm-providers.md; tool execution in tool-catalog.md; this file
is the loop that ties them together.

## Thread and cancellation setup

Keep one `threading.Event` per chat in a lock-guarded dict. A new send
for the same chat SETS the old event first (the superseded run winds
down), then registers its own and starts the worker:

```python
_cancels: dict[str, threading.Event] = {}
_streams: dict[threading.Event, object] = {}   # live HTTP responses
_lock = threading.Lock()

def send(chat_id, provider, model, messages, push, context):
    cancel = threading.Event()
    with _lock:
        if (old := _cancels.get(chat_id)):
            old.set()
        _cancels[chat_id] = cancel
    threading.Thread(target=work, daemon=True,
                     name=f"agent-{chat_id[:12]}").start()
```

The hard-won detail: a cancel Event CANNOT interrupt a read blocked on
a socket. Register every live streaming response keyed by the run's
cancel Event; `stop(chat_id)` sets the Event AND closes the response,
which unblocks the read immediately instead of waiting out its
timeout. Gate waits (below) poll the Event themselves.

```python
def stop(chat_id):
    with _lock:
        ev = _cancels.get(chat_id)
        resp = _streams.get(ev) if ev else None
    if not ev:
        return False        # no live run - the UI heals its own state
    ev.set()
    if resp is not None:
        try: resp.close()   # unblocks a blocked read NOW
        except Exception: pass
    return True
```

## Turn structure

Each model turn streams text (pushing `delta`/`thought` events as
chunks arrive) and accumulates tool calls, returning
`(text, tool_calls, raw_assistant_msg)` where the raw message is in
the provider's native history shape. The loop:

```python
while True:
    if cancel.is_set(): break
    try:
        text, calls, amsg = model_turn()
    except ChatError as e:
        if cancel.is_set() or not transient(e): raise
        emit({"kind": "retry", "detail": str(e)})
        if cancel.wait(RETRY_BACKOFF_S): break   # stoppable backoff
        text, calls, amsg = model_turn()
    history.append(amsg)
    if not calls or cancel.is_set(): break
    emit({"kind": "turn_break"})    # UI closes the current text bubble
    for call in calls:
        if cancel.is_set(): break
        ...gate, execute, emit tool_result, append result msgs...
```

Grant ONE backoff-retry per turn on transient errors only: HTTP
408/409/429/5xx, unreachable/connection-failed, stream stalls. 4xx
auth/bad-request re-fails identically - never retry those. History
already holds every completed tool result, so re-asking the model
loses nothing. Back off with `cancel.wait(3)`, not `sleep` - Stop
must work mid-backoff.

On the turn budget: this design deliberately runs UNBOUNDED. The user
is the circuit breaker - Stop works mid-read and mid-approval, every
tool call is visible as it happens, and the context meter shows
growth. A hard cap silently truncates legitimate long tasks; if you
add one anyway, surface "budget reached" as its own event, never as a
generic error.

## The permission gate

Permissions are resolved by the FRONTEND - that is where config and
per-chat overrides live. The backend simply blocks until the UI
answers Allow or Deny (an "Ask" policy shows a card and the answer is
whatever the user clicks). The gate is a pending-map of Events:

```python
class ToolGate:
    def prepare(self, call_id):            # BEFORE the event is emitted
        with self._lock:
            self._pending[call_id] = {"event": threading.Event(),
                                      "decision": None}
            if call_id in self._early:     # answered before prepare
                rec = self._pending[call_id]
                rec["decision"] = self._early.pop(call_id)
                rec["event"].set()

    def wait(self, call_id, cancel=None):
        rec = self._pending.get(call_id)
        if rec is None: return "deny"
        while not rec["event"].wait(0.25):
            if cancel and cancel.is_set(): break
        ...pop record; return decision if answered else "deny"...

    def answer(self, call_id, decision):
        rec = self._pending.get(call_id)
        if rec is None: self._early[call_id] = decision  # buffer it
        else: rec["decision"] = decision; rec["event"].set()
```

Three ordering rules that make this correct:

- `prepare()` runs BEFORE the `tool_call` event is emitted, so a fast
  UI answer can never race ahead of registration. The early-answer
  buffer is belt-and-suspenders for the same race.
- `wait()` has deliberately NO timeout. An unattended approval waits
  forever; auto-denying after a delay reports "denied by user" for a
  decision the user never made - a lie in the transcript. The card
  stays on screen, Stop works mid-wait, and a restart re-asks via the
  resume path (below).
- The wait polls in 250 ms slices so the cancel Event is honored;
  an unanswered or cancelled wait resolves to "deny".

A denied call still feeds a result to the model:
`{"ok": False, "detail": "The user denied permission to run X."}` -
the model must learn the call failed and why, or it re-issues it.

## Frontend-executed tools

Some tools mutate state the FRONTEND owns (e.g. creating a chat or
relaying a message when chat metadata is persisted by the page as one
list - a backend write would be clobbered by the page's next
whole-list save). For those, after approval the loop emits a
`tool_exec` event and blocks on a second gate identical in shape to
ToolGate but carrying a result dict instead of a decision. The page
executes the action and reports `{ok, summary, detail}` back through
a bridge call. Same no-timeout rule; a cancelled wait returns
`{"ok": False, "summary": "cancelled", ...}`, and a malformed result
becomes an honest failure rather than a crash.

## Executing and feeding back

Per call, in order: resolve which executor owns the tool (repository
toolset vs app-wide toolset - tool-catalog.md), compute the
human-readable summary and a PREVIEW (params as key/value pairs, a
proposed diff for edits) so the approval card shows exactly what will
happen, `gate.prepare(id)`, emit `tool_call`, `gate.wait(id, cancel)`,
execute or deny, emit `tool_result`, then append the result to the
provider-native history and continue. Approval previews must never
truncate values - the user approves what they can read.

Cap the detail fed back to the model with a large BACKSTOP only
(e.g. 256 KB). Every tool already bounds its own output against the
model's context budget and ends big reads with a "call again with
start_line=N" hint; a flat low cap here would silently eat a budgeted
read slice INCLUDING its paging hint - the model could neither see the
content nor learn how to fetch the rest. Image-bearing results are
delivered provider-natively: some APIs accept image blocks inside the
tool result; text-only tool channels get a follow-up user message
carrying the image plus a "[image content of the X result above]"
marker (llm-providers.md).

## Cancellation semantics

Stop interrupts everything: a socket read (response closed), a retry
backoff (`cancel.wait`), a permission wait, a frontend-exec wait, and
the loop checks between turns and between calls. Two invariants:

- Partial output is KEPT. Text already streamed stays in the
  transcript; the UI freezes the live bubble on `done`.
- A cancelled run ends as `done {cancelled: true}`, NEVER as an
  error. Closing the stream surfaces as a read error inside the turn,
  so every exception handler checks `cancel.is_set()` first and
  reports cancellation instead.

The UI adds a watchdog: after requesting stop, if no done event lands
within ~8 s it forces the chat idle locally so the interface is never
stuck on "stopping" (the daemon thread dies with the app). If the
backend reports no live run for a chat marked running (crash ghost),
the UI heals to idle on its own.

After an app restart, chats persisted as running are ghosts - no loop
survives the process. Normalize them to idle, then RESUME: re-send
with a model-facing bridge note describing exactly how the turn was
cut ("your tool call X was awaiting approval and was NOT executed -
call it again; the normal approval flow applies" / "X was executing
and its outcome is UNKNOWN - verify state before repeating
side-effecting steps" / "you were cut off mid-response - continue,
do not repeat"). Show the note verbatim in the transcript too: the
user sees everything the model was told.

## Event vocabulary (transcript contract)

Every event is one flat JSON object pushed to the page (ui-bridge.md),
tagged `type: "chat"` and `chatId`. The kinds, as a convention:

- `turn_start {model, msgs, tools}` - request on the wire; the UI
  stamps arrival time and measures TTFT to the first streamed content
- `delta {text}` / `thought {text}` - streamed answer / reasoning
- `turn_break` - text span over, tool calls follow (close the bubble)
- `tool_call {callId, tool, perm, args, summary, params,
  previewDiff, note, repo, branch, repoMode}` - approval card input
- `tool_exec {callId, tool, args}` - page-executed tool, post-approval
- `shell_output {callId, text}` - live line from a running command
- `tool_result {callId, ok, summary, detail, diff, sha, state}`
- `usage {inTotal, out, cacheRead, cacheWrite, reasoning}` - provider
  token truth per turn (anchors the UI's context estimate)
- `retry {detail}` - transient failure, one re-ask in flight
- `done {cancelled}` / `error {detail}` - exactly one ends every run

Error surfacing: expected failures (provider errors, bad config)
raise a domain exception whose message becomes `error.detail`;
unexpected exceptions become `error {detail: "TypeError: …"}` with a
server-side traceback print - the loop thread must never die silently.
The frontend renders `error` as a visible ⚠ transcript entry and
returns the chat to idle.

## Rules

- One daemon thread per in-flight message; a new send supersedes the
  old run by setting its cancel Event. Never run the loop on the UI
  bridge thread.
- `gate.prepare()` before emitting `tool_call` - always. The
  early-answer buffer covers the remaining window.
- No timeout on permission or frontend-exec waits; the user (Stop,
  restart-resume) is the escape hatch. A fabricated timeout answer is
  a lie in the transcript.
- Cancel Events cannot unblock socket reads - track live responses
  and close them in stop().
- `cancel.is_set()` first in every except path: cancelled runs end
  `done(cancelled)`, never `error`.
- Retry only transient errors, once per turn, with a cancellable
  backoff; 4xx auth errors surface immediately.
- Feed denied calls back as failed results; never drop a call the
  model made or it will repeat it.
- Keep the result backstop cap ABOVE every tool's own output budget,
  or paging hints get eaten (streaming-cursor.md covers the render
  side of these events).
