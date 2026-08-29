# Build plan — walking through the system reliably

A distributed database is too big to build in one pass. This is the
milestone ladder: each rung is a working, tested system that the next
rung extends. Do the rungs IN ORDER; each has an explicit gate — do not
start the next milestone until the gate passes. (General planning
method: see `engineering/project-planning.md` in this knowledge base.)

## Why a ladder and not a big design

- Every milestone is demoable and revertable; there is never a month of
  code that "will work once it's all connected".
- Bugs localize: when rung 5 fails, rungs 1–4 are already trusted, so
  the search space is one rung.
- The test harness grows one capability per rung, in step with the code
  it tests.

## The ladder

### M1 — Storage engine wrapper (single node, no network)
Build the `Engine` interface over Pebble (pebble.md), with the key
schema and the applied-index batch trick already in place (even though
nothing replicates yet — retrofitting idempotence later is misery).
**Gate:** property test vs a Go map passes (testing.md); crash-restart
test: kill -9 mid-writes, reopen, engine state equals a prefix of the
acknowledged writes; `-race` clean.

### M2 — Single-node server with a wire protocol
Put the engine behind the client protocol (wire-protocols.md):
Get/Put/Delete/Scan, request IDs, deadlines. Add the fuzz test for the
decoder the day the decoder exists.
**Gate:** protocol fuzz runs clean; a client round-trips 100k ops;
graceful shutdown drains in-flight requests (goleak clean).

### M3 — One raft group (replicated KV)
The Ready loop (etcd-raft.md), in-memory test transport first, real
transport second. State machine = M1 engine. This is the hardest rung —
budget for it and keep the scope pure: no shards, no snapshotting to
followers yet if it helps, single static 3-node membership.
**Gate:** in-process 3-node harness (testing.md) passes: leader
election, replicated writes, follower restart catch-up, partition of
the leader with no lost committed write; invariant check (all nodes
applied identical prefixes) green over 1000 randomized ops.

### M4 — Raft maintenance: snapshots, compaction, membership
Snapshot install for slow/new followers, log compaction, add/remove
node via conf change with the learner→voter promotion.
**Gate:** a wiped node rejoins via snapshot and converges; log on disk
stays bounded under sustained writes; membership change under load
loses nothing.

### M5 — Metadata group + routing (still one data shard)
Split roles: the metadata group (metadata-group.md) holds the node
registry and a shard table with ONE shard; clients route via the cached
shard map and handle NotLeader/WrongShard. Nothing moves yet — this
rung only introduces the indirection.
**Gate:** clients bootstrap from metadata, survive data-group leader
failover without config changes, and epoch-stale clients self-correct.

### M6 — Multi-group + static sharding
Run many data groups per process (multigroup-raft.md); the shard table
maps ranges to groups (sharding.md), created statically at cluster
init. No splits yet.
**Gate:** N shards across 3 nodes serve disjoint ranges; per-group
failover is independent (killing one group's leader stalls only that
shard's writes in the harness).

### M7 — Shard split and rebalance
The split state machine and replica moves (sharding.md), driven by the
metadata group, resumable at every step.
**Gate:** harness kills the coordinator at EVERY step boundary of a
split and a move; the operation always completes or rolls back on
restart; no key is ever lost or served by two shards.

### M8 — Blob store
Chunking, manifests in the KV, chunk placement, scrub/repair, GC
(blob-storage.md) with replication first, erasure coding
(erasure-coding.md) second.
**Gate:** kill a node, scrub reconstructs its chunks elsewhere; GC
never collects a chunk reachable from any manifest (property test:
mark-sweep vs reference graph); erasure-coded read succeeds with any
`parity` shards missing.

### M9 — Users, auth, frontends
Accounts and tokens in the metadata group (user-systems.md), admin UI
and client API surfaces (frontends.md).
**Gate:** authz property test (no operation succeeds without the
permission that names it); admin UI renders cluster/shard/node state
from a live harness cluster.

## Rules for walking the ladder

1. **One rung in flight at a time.** If a lower rung breaks, fix it
   before continuing — the ladder's value is that lower rungs stay
   trusted.
2. **Gates are executable**, not judgment calls: each is a test (or
   small suite) that stays in CI forever after its rung. The suite at
   M9 still runs the M1 property test.
3. **When a rung is too big, split the rung**, not the discipline:
   e.g. M3 into "Ready loop against fake transport" → "real transport"
   → "restart persistence". Each sub-step still ends green.
4. **Write the harness capability before the feature**: partition
   support lands in the test transport before partition handling lands
   in the node. Untestable features wait.
5. **Keep a STATUS.md** in the repo: the current rung, what's done,
   what the next gate needs. Any session (human or agent) can resume
   from it without archaeology — update it every time a gate passes.
6. **No speculative generality.** Rung N may only add abstractions that
   rung N needs. The seams in this folder's README are the exception:
   they exist precisely so later rungs don't force rewrites.
