# Sharding — ranges, splits, moves, rebalancing

How the key space is divided across raft groups, and the protocols that
change the division safely while serving traffic. Everything here is
driven by the metadata group (metadata-group.md) and executed by data
nodes (multigroup-raft.md).

## Range sharding (the default here)

A shard = a half-open key range `[start, end)` owned by one raft group.
The shard table is a sorted list of ranges covering the whole key space.

- Routing is a binary search over sorted start keys.
- Splits are natural: cut a range at a key.
- Range scans stay contiguous — one shard (or few) per scan.

The alternative, hash sharding (route by `hash(key) % slots`), spreads
load evenly and needs no splits, but kills ordered scans and makes
resharding move a fraction of EVERY shard. Use hash placement for blob
chunks (blob-storage.md, no ordering needed); use ranges for the KV.
Hot sequential writers on range sharding are real — mitigate at the key
schema level (prefix by tenant/bucket) rather than switching schemes.

## When to split

Split shard S when (checked periodically by the metadata leader from
node-reported stats):

- size: S exceeds a byte budget (e.g. 8–64 GiB), or
- load: S sustains a hot-spot QPS threshold.

Split key choice: the range's median by size — engines can estimate
from their own stats (Pebble table properties); a sampled middle key is
fine to start.

## The split protocol (crash-proof, resumable)

Goal: `S [a,c)` becomes `S1 [a,b)` and `S2 [b,c)`. The insight that
makes it cheap: every replica of S ALREADY HOLDS the data of both
halves — a split creates a new raft group on the SAME nodes and fences
the ranges; no data copies.

Each step is a metadata proposal (`op/<id>` records progress; any new
metadata leader resumes — see metadata-group.md):

1. **Plan**: propose op {split S at b}. Apply-time check: S is
   `normal`; set S `state: splitting`. Epoch bump.
2. **Create S2's group**: instruct S's replica nodes to create raft
   group G2 (multigroup-raft.md lifecycle) with an initial state that
   is a lightweight marker "data lives under G1's prefix until
   cutover" — or simpler: G2 members are created empty and step 3
   copies. SIMPLEST CORRECT VERSION: iterate engine range [b,c) into
   G2 via its own proposals while S still serves [a,c). Record
   copied-up-to-key in op state so a crash resumes the copy, and
   tee writes ≥ b to both during the copy (dual-write window).
3. **Cutover**: single metadata proposal that atomically (it is one
   apply): sets S range to [a,b), inserts S2 [b,c) → G2, bumps epoch,
   sets both `normal`, closes the op. From this epoch, S rejects keys
   ≥ b with WrongShard; clients refetch and route to S2.
4. **Cleanup**: G1's replicas range-delete [b,c) from G1's prefix;
   G2's dual-write markers dropped. Pure garbage collection — safe to
   redo any time.

Merging (rarely needed) is the mirror: copy small S2 into S1, cutover,
delete G2 — implement only when shrink actually matters.

## Moving a replica (rebalance / repair)

Move shard S's replica from node A to node B (because A is full, dead,
or draining):

1. Metadata proposes op {move S: A→B}, checks S `normal`, sets
   `moving`.
2. **Add B as learner** to S's raft group (conf change through S's own
   log — etcd-raft.md). B receives a snapshot
   (`Engine.SnapshotForRange`) + log tail.
3. When B is caught up (leader reports match index), **promote B to
   voter** (conf change).
4. **Remove A** (conf change; transfer leadership away from A first if
   it leads).
5. Metadata records the new replica set, bumps epoch, sets `normal`;
   A eventually garbage-collects the group (tombstone —
   multigroup-raft.md).

The group is never below full voter strength: add before remove.
Learner-first means the quorum never depends on the newcomer.

## Rebalancing policy

A background loop on the metadata leader:

```
score(node) = bytes used / capacity          (or shard count to start)
while max(score) - min(score) > threshold:
    pick shard on max-node whose move shrinks the gap most
    if no op running for it: start a move op (one or two at a time!)
```

Rules: cap concurrent moves (they consume disk+network), never move a
shard with an op in flight, prefer moving leaders last, and make the
loop observable (frontends.md: current ops, queue, last decisions).
Repair (replacing replicas of a dead node) is the same move mechanism
with higher priority and the source replica skipped.

## Client rules (the contract that ties it together)

- Cache the shard map + epoch; route by range.
- On `NotLeader{hint}`: retry at hint (bounded retries).
- On `WrongShard{epoch}`: refetch map if newer, re-route. During the
  dual-write window of a split both owners accept — the epoch check on
  the SERVER is what keeps this safe, clients stay dumb.
- Data nodes verify ownership (range + epoch) on every write before
  proposing it to their group. This check is the single load-bearing
  guard of the whole scheme — test it under every protocol step
  (build-plan.md M7 gate).
