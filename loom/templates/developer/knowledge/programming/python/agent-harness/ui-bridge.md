# The UI bridge at harness scale - events, gates, many windows

../pywebview/js-api-bridge.md covers the basics: explicit methods on
the `js_api` object, every JS call is an awaited Promise, returns are
JSON. A workbench needs more than that contract: a JsApi with a
hundred methods that stays maintainable, a channel for PUSHING events
from any backend thread into the page, backend threads that BLOCK
until the page answers (tool approvals, credential prompts,
page-executed tools), several OS windows sharing one bridge, and a
`file://` frontend assembled without a bundler. This file is those
mechanisms; the agent loop that drives most of the traffic is
agent-loop.md, and the page-side consumption is
../../web/js-app-architecture.md.

## Organizing a large JsApi

One class, one flat surface - pywebview exposes methods by name, so
nesting buys nothing. Structure comes from comment banners grouping
methods by domain (app, repos, chat, windows, environments, …) and
from ONE wrapper that gives every method the same envelope:

```python
def _api_call(fn):
    def inner(*a, **kw):
        try:
            out = fn(*a, **kw)
            return out if isinstance(out, dict) and "ok" in out \
                else {"ok": True, "data": out}
        except DomainError as e:          # expected: clean message
            return {"ok": False, "error": str(e)}
        except Exception as e:            # bridge must never die
            traceback.print_exc()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return inner
```

Methods stay one-liners delegating to modules
(`return _api_call(repos.list_repos)()`). Domain modules raise their
own exception types; a per-domain adapter maps them onto the common
one so the envelope never grows variants. The frontend wraps the
whole surface in one `call()` helper with a mock fallback for
plain-browser work (../../web/js-app-architecture.md).

Anything slow returns IMMEDIATELY and finishes via events: spawn a
daemon thread, hand back `{ok, opId}`, and push
`op {id, state: running|done|error, label, detail}` as it progresses.
A bridge method that blocks for seconds freezes every pending call
from that page.

## The event channel (backend → page)

Mechanism: `window.evaluate_js()` calling ONE global entry point that
the page defines and fans out from:

```python
payload = f"window.onBackendEvent && onBackendEvent({json.dumps(ev)})"
```

Two details carry all the weight. `json.dumps` the WHOLE event - its
output is a valid JS object literal with quotes and newlines escaped;
never interpolate raw values into the script string. And the
`window.X && X(...)` guard makes an event that arrives before the
page finishes loading a silent no-op instead of a JS error.

Threading is the hard-won part. `evaluate_js` blocks until the Qt
MAIN loop executes the script. Consequences:

- Calling it from the Qt main thread self-deadlocks.
- Calling it from a pywebview JS-bridge thread (pywebview creates
  those NON-daemon) can hang process exit forever: if Ctrl+C kills
  the Qt loop mid-send, the semaphore inside `evaluate_js` is never
  released and the interpreter joins the stuck thread at shutdown.

So: never `evaluate_js` on the calling thread. Route every send
through ONE dedicated daemon thread draining a FIFO queue - ordering
stays exactly as strict as direct calls, and a send caught mid-flight
by teardown can never block exit:

```python
class Bus:
    def __init__(self):
        self._window = None            # main window (set once loaded)
        self._popouts = {}             # key -> secondary window
        self._backlog = []             # events before the window exists
        self._lock = threading.Lock()
        self._q = queue.SimpleQueue()
        threading.Thread(target=self._sender, daemon=True).start()

    def _sender(self):
        while True:
            win, script = self._q.get()
            try:
                win.evaluate_js(script)
            except Exception:
                pass                   # window died - the event is moot

    def push(self, ev):                # -> main window (or backlog)
    def push_to(self, key, ev):        # -> one registered window
    def broadcast(self, ev):           # -> every window
```

`push` before the main window exists appends to a backlog that
`set_window()` flushes - startup code (first-run seeding, early
notifications) never needs to know whether the page is up yet. Give
the Bus to backend modules as a plain callable (`push=bus.push`) so
they stay import-clean of the UI layer.

## Blocking gates (backend waits on the page)

The inverse flow: a backend thread parks until the frontend answers.
The shape is always a lock-guarded pending map of
`call_id -> {threading.Event, answer}`:

```python
def prepare(cid):   # register BEFORE emitting the event
def wait(cid, cancel=None):
    while not rec["event"].wait(0.25):       # poll so cancel works
        if cancel and cancel.is_set(): break
    # pop record; return the answer, or the refusal default
def answer(cid, value):                      # from a bridge method
    rec["decision"] = value; rec["event"].set()
```

`prepare()` MUST run before the event is emitted, or a fast click
races registration; buffer answers for unknown ids as backup. The
0.25 s poll slices exist because a plain `Event.wait()` cannot
observe the run's cancel Event.

Three instances, two timeout policies:

- **Tool approval** and **page-executed tools** (`tool_call` /
  `tool_exec` events, answered via `tool_answer` /
  `tool_exec_result` bridge methods): deliberately NO timeout. The
  answer is a statement about what the user decided - fabricating
  "denied" after a delay is a lie in the transcript. Stop (the cancel
  Event) is the escape hatch. Full loop-side treatment in
  agent-loop.md.
- **Credential prompts**: `git`/`ssh` subprocesses need a passphrase
  on a TTY that doesn't exist. Ship a tiny helper script into
  `~/.yourapp/bin/`, point `GIT_ASKPASS`/`SSH_ASKPASS` at it; when
  invoked it connects to your unix socket (0600, per-pid path under
  `~/.yourapp/run/`), sends the prompt text, and blocks. The broker
  thread serving that socket pushes a `prompt {id, text}` event,
  waits on its Event, and writes the answer back to the helper's
  stdout. HERE a timeout is correct (~170 s, just under the helper's
  own 180 s socket timeout): a third process is holding a connection
  and will give up anyway - expiring returns "no secret" and git
  fails cleanly. Cancel from the modal answers with `None` (helper
  exits nonzero, git aborts); "remember" caches the answer IN MEMORY
  keyed by prompt text - never on disk (secret-envs.md owns durable
  secrets) - so one entry covers a whole fetch/push burst.

## Multi-window apps on one bridge

Pass the SAME JsApi instance as `js_api=` to every
`create_window()`. One surface, no per-window duplication - which
means every method must be callable from any window: identify the
caller by explicit arguments (`repo`, `branch`, `chatId`), never by
"the window". A working split of window kinds:

- **Main window** - owns app-level state (chat metadata, live
  streams) and the system tray; the Bus's default `push` target.
- **Popout editors** - exactly one per (repo, branch); a registry
  keyed `f"{repo}\x00{branch}"` enforces it. Opening an existing key
  FOCUSES the window (Qt raise/activate, bounced to the Qt main
  thread) and forwards the intent as an `open_file` event via
  `push_to` - never silently drop what the user asked for.
- **Singleton docs window** - one slot; reopen focuses and jumps it
  by evaluating a small navigation call. When `evaluate_js` raises,
  the window died: clear the slot and recreate.
- **Per-chat diagnostics windows** - any number, keyed by chat id.

Each secondary window registers on the Bus at create and unregisters
in its `closed` handler. Each page filters the event types it cares
about; `broadcast` carries cross-cutting facts (e.g. a commit hook
broadcasting `{type: "depot", kind: "commit", repo, branch, sha}` so
every window refreshes its tree).

Pages cannot talk to each other - relay through the backend. The
diagnostics pattern: the popout announces `diag_ready` (bridge call →
`push` to main), the MAIN window computes the live payload it alone
owns and calls `diag_feed` (bridge call → `push_to` the popout). Keep
the steady feed compact; full detail for one entry travels the same
relay on click. On popout close, push a "stop feeding" event so the
main window quits computing for nobody.

Dirty state and quit: popouts report their dirty-buffer count over
the bridge; a popout's `closing` handler returns `False` and pushes
`confirm_close` when buffers would be lost; ONE shared quit gate sums
dirty counts across windows before anything closes, with a force flag
for "user already confirmed". Persist the open-popout list under
`config["session"]` on every open/close - but NOT during quit, or
closing-all would erase the very state the next launch restores
(persistence.md). Restore runs on a short timer AFTER the main
window's `loaded` event: `create_window` must not run inside a
`loaded` handler.

## Startup order

1. Parse CLI (`--help` runs nothing), ensure the data dir exists.
2. Single-instance gate via a pidfile (live PID → exit; stale →
   clean and proceed) - BEFORE detaching, so the message lands in
   the caller's terminal.
3. Detach from the terminal (fork+setsid, stdio → a logfile) unless
   `--foreground`/`--debug`. MUST precede any thread or Qt object:
   fork does not carry threads, and Qt state must never cross one.
4. Write the pidfile (post-fork, so the PID is final), compose the
   frontend (below), start the Bus and prompt broker, run first-run
   seeding and config migrations.
5. `create_window` with `Path.as_uri()`, `api.set_window(window)`
   (flushes the Bus backlog), hang tray/DevTools/restore handlers off
   `window.events.loaded`, then `webview.start(gui="qt")` - which
   blocks until the last window closes; cleanup in `finally` plus
   `atexit` for paths that skip it.

## Composing the file:// pages at launch

`file://` blocks ES-module imports and `fetch()` of sibling files,
but classic `<script src>` and `<link rel=stylesheet>` work
(../pywebview/app-skeleton.md has the tradeoff table). So keep CSS/JS
as plain files and generate only the HTML shells: render
`templates/*.html.j2` with Jinja2 at EVERY launch - about 1 ms, no
bundler, no npm, no watcher; restarting the app (which a no-hot-
reload frontend needs anyway) re-renders. One template per window
kind (main, editor popout, docs, diagnostics), each with its own
explicit, ORDERED script list - plain script tags mean later files
use globals defined by earlier ones, and the order in one Python list
is the whole dependency system. Use `StrictUndefined` so a template
typo fails the launch loudly, and keep a
`python -m your_app.compose` entry point for regenerating by hand.

Anything the page would want to `fetch()` gets embedded at compose
time instead: bundle Markdown docs as a JSON blob in the page, inline
images as `data:` URIs. Give popout pages the same mock-data seeds
the main page uses so any page still opens usefully in a plain
browser.

## Rules

- Never `evaluate_js` on the calling thread - one daemon FIFO sender
  thread, exceptions swallowed (a dead window makes the event moot).
- `json.dumps` the entire event into the script; never f-string a
  value in. Guard with `window.fn && fn(...)`.
- Gate `prepare()` before emit, always; buffer early answers.
- No timeout on user decisions; timeout only when an external
  process bounds the wait anyway (askpass: stay under the helper's
  own socket timeout).
- One shared JsApi for all windows; methods take explicit identity
  args and long work leaves the bridge thread immediately.
- Qt object work (focus, tray, icons) always hops to the Qt main
  thread via `QTimer.singleShot(0, QApplication.instance(), fn)`,
  and callbacks running inside Qt's event loop must never raise.
- Pin Python's CYCLIC gc to the Qt main thread (`gc.disable()` +
  a QTimer running `gc.collect()` every ~10 s): a Qt widget wrapper
  collected on a worker thread destroys its C++ widget there, which
  aborts the process on Wayland.
- Windows die at any moment: wrap every window operation, clean the
  registry in `closed` handlers, recreate on a raised send.
- Registered-window keys are backend-internal - build them with a
  separator that can't appear in the parts (`"\x00"`), not `"-"`.
