# Virtual providers — routing bundles with priority, capacity, and failover

A virtual provider is a user-defined NAME over a set of ROUTES, each
pinning one concrete provider instance + model (llm-providers.md).
A chat that selects one stores only the virtual id — every send
resolves to a concrete (provider, model) at request time. This turns
"my main account, spill to the local server, fall back to the cheap
tier" into configuration instead of manual model-switching, and it
gives the app one place to queue work when everything is busy and to
fail over when a route dies.

## Config shape

Definitions live in config (`virtualProviderList`); each:

```json
{"id": "vp-3fa9c2d1", "name": "daily driver", "enabled": true,
 "routes": [
   {"id": "rt-a1b2c3d4", "provider": "prov-…", "model": "…",
    "priority": 10, "capacity": 2},
   {"id": "rt-e5f6a7b8", "provider": "prov-…", "model": "…",
    "priority": 20, "capacity": 1}]}
```

Two knobs, MX-record semantics:

- `priority` — LOWEST number preferred; higher numbers are
  fallbacks. Equal priorities load-balance among themselves.
- `capacity` — how many requests the route may carry AT ONCE
  (min 1). Saturated routes spill to the next candidate; when every
  route is saturated the send WAITS, cancellably.

Validate at save time, loudly: every route needs a provider and
model, the provider must EXIST, capacity ≥ 1, and routes must
target concrete instances — virtual providers never nest (nesting
turns routing into recursion with no coherent capacity story).
When a concrete provider is deleted, drop its routes from every
bundle and tell the user which bundles were touched.

## Selection

Candidates are re-read on every attempt (config can change while a
send waits). Filter first: excluded routes (already failed this
send) and routes whose provider is removed or disabled never
receive traffic. Then sort:

```python
cands.sort(key=lambda r: (r["priority"],
                          inflight.get(r["id"], 0) / r["capacity"],
                          (hash(r["id"]) + rotor) % 97))
```

Priority ascending; within a tier, RELATIVE load (inflight ÷
capacity, so a big route and a small route fill proportionally);
ties broken by a rotating hash so equal idle routes share work
instead of the first one taking everything. The first candidate
with `inflight < capacity` wins the slot.

## Acquire: lease, or queue and wait

All live state sits under one `threading.Condition`. Acquiring
returns a LEASE — a granted slot on one route — or `None` when the
user cancelled while waiting:

1. Compute candidates; if the send may claim (see below), take the
   first with a free slot, increment its inflight count, return a
   lease recording route, reason, and wait time.
2. Otherwise: if cancelled, return None. If not yet queued, append
   a ticket to the bundle's FIFO queue and notify waiters of their
   positions. Then `cond.wait(timeout=1.0)` and loop.

The claim rule is what keeps the queue honest: only the queue HEAD
may claim a freed slot, and a NEWCOMER may claim immediately only
when nobody is queued. Without it, a request arriving just as a
slot frees races the head's wakeup and snipes the slot — arrival
order silently breaks. On grant, remove the ticket, re-notify
everyone behind (they all moved up), and `notify_all` so the new
head re-checks immediately.

Waiters get a notice callback on EVERY queue mutation — join,
grant, cancel, boost — carrying `{position, total, detail}`
(1-based), so on-screen positions are never stale. Notices must
only enqueue UI events, never block: they run under the lock.

Cancellation: the ticket is removed in a `finally` — a cancelled
or crashed waiter must not haunt the queue, or it blocks everyone
behind it forever. A `boost(vid, chat_id)` operation moves a
queued send to the FRONT ("this one next" beats arrival order);
the boost-vs-grant race is fenced by the same lock — if the send
was already granted there is no ticket left and boost reports
that instead of touching anything.

The lease records WHY it was granted — `preferred` (first choice,
free), `spillover:capacity` (a better-or-equal candidate was
full), `waited:capacity` (queued first), `failover:error` (a
previous route failed this send). `lease.done(outcome)` runs
exactly once: decrement inflight, update tallies (served, errors,
total duration, last-use timestamp), `notify_all` to wake the
queue, and append the observation record.

Light one-shot calls (title/summary helpers) use a resolve-only
path: best candidate right now, NO slot held, no waiting — a
two-second title request must never queue behind agent turns.

## Failover — and the visible-output line

The send wrapper loops: acquire a lease (excluding routes already
tried), run the full agent turn on it, and on failure decide:

```python
try:
    run(lease.provider, lease.model)
except BaseException as e:
    lease.done("cancelled" if cancel.is_set() else "error", str(e))
    if not transport_error(e) or cancel.is_set() or visible_output:
        raise                      # surface it — do NOT reroute
    tried.add(lease.route_id)
    emit retry event; continue     # next route
```

The load-bearing condition: failover happens ONLY before anything
user-visible has streamed. The flag flips on the first `delta`,
`thought`, or `tool_call` event. After that point, rerouting means
REPLAYING the turn on another model — which would re-run tools the
user already watched execute (edits, shell commands: real side
effects, done twice), and would silently splice two models' output
into one answer. Before first visible output a retry is invisible
and harmless; after it, the error must surface and the user
decides. Cancellation always wins over rerouting, and only
transport-class errors (ChatError) are failover-worthy — a code
bug on route A will be a code bug on route B.

Each failover emits a UI event naming the failed route and error,
and the final RouteError carries the last route's error text — "
every route failed" alone is undebuggable.

## Config vs live state

The split is strict and deliberate:

- CONFIG: definitions only (routes, knobs). Survives restarts,
  syncs like any other setting.
- LIVE STATE: inflight counts, served/error tallies, latency sums,
  queue contents — in-memory, describing THIS process only. Gone
  on restart, and that is correct: an inflight count persisted to
  disk would leak slots after a crash and deadlock the queue.

A usage snapshot — per route: inflight, served, errors, average
latency, last use; per bundle: total inflight/capacity and queue
depth — feeds the UI through an `on_usage` callback fired on every
change (grant, done, queue mutation, config edit). The callback is
best-effort: wrap it in try/except, because a display bug must
never kill the routing that feeds it. The UI patches these numbers
into place live rather than re-rendering pickers wholesale.

## The observation log

Every finished lease appends one JSON object to an append-only
`routing.jsonl` in the app's data directory: timestamp, chat id,
bundle id/name, route id, provider, model, priority, reason,
wait ms, duration ms, outcome (`ok`/`error`/`cancelled`), error
text truncated to ~300 chars. Guard the append with a lock and
swallow OSError — analytics must never break the chat that
produced them. Nothing reads the log yet; it exists from day one
so a future latency/cost/error-aware router starts with history
instead of a cold start.

## Rules

- Routes target concrete instances only — never let bundles nest.
- Validate at save time; a route that can never dispatch must not
  be saved silently.
- One Condition guards inflight counts, stats, and queues; every
  mutation notifies. Sort by (priority, inflight/capacity,
  rotating tie-break).
- Only the queue head claims a freed slot; newcomers claim only on
  an empty queue. Remove tickets in `finally`.
- `done()` exactly once per lease; make it idempotent anyway.
- Failover only on transport errors, only before the first
  visible delta/thought/tool_call, never after cancellation.
- Live counters are per-process and in-memory; persisting them is
  a bug, not a feature.
- Emit route/queue/retry events for everything (agent-loop.md
  renders them via streaming-cursor.md) — silent routing reads as
  a hung app.
