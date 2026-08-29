# The metadata group

One dedicated raft group — group 0 by convention — that holds the
cluster's control-plane truth. Every other component ASKS it or CACHES
it; nothing else decides placement or membership. This single decision
is what keeps a sharded system coherent: there is exactly one place
where "which shard owns key K, and where are its replicas" is decided.

It is an ordinary raft group built exactly like etcd-raft.md (usually
placed on 3 or 5 designated nodes); only its state machine differs.

## What lives in it

```
node/<id>            NodeInfo{addr, capacity, state: active|draining|dead}
shard/<id>           ShardDesc{range [start,end), groupID, replicas [](node,role),
                     state: normal|splitting|moving, epoch}
shardmap/epoch       uint64 — bumped on EVERY shard-table change
op/<id>              long-running ops (split/move) with their step state
user/<name>          accounts + grants (see user-systems.md)
config/<key>         cluster-wide settings (replication factor, EC scheme…)
```

Everything is small, low-write-rate control data. Bulk data NEVER goes
through the metadata group — that is what data shards are for.

## Responsibilities

1. **Node registry + liveness.** Nodes register at startup and
   heartbeat (every few seconds, through normal proposals or leader
   lease reads + batched updates). Liveness state drives repair: a node
   `dead` for longer than a grace period triggers replica re-placement
   (sharding.md) and chunk repair (blob-storage.md).
2. **Shard table.** The authoritative range → group → replicas map.
   All changes (create, split, move) are proposals here, and every
   change bumps `shardmap/epoch`.
3. **Long-running operation state.** Splits and moves are multi-step;
   each step transition is a metadata proposal (`op/<id>` records the
   current step). Because the state machine is replicated, the
   coordinator can die and any new metadata leader resumes the op from
   its recorded step — this is what makes sharding.md's protocols
   crash-proof.
4. **Placement policy.** Given capacities and current spread, choose
   nodes for new replicas/chunks. Keep the policy a pure function
   `place(need, nodes, existing) -> []NodeID` so it is unit-testable.

## Routing: how clients and nodes use it

- **Bootstrap:** a client connects to any known address, asks the
  metadata group for the shard map (+epoch), caches it.
- **Steady state:** route every request by cached map — binary search
  the sorted ranges for the key's shard, send to its leader replica.
  Zero metadata traffic on the hot path.
- **Correction:** every data-node response carries the node's view of
  the shard epoch. Two self-healing errors:
  - `NotLeader{leaderHint}` → retry at hinted replica.
  - `WrongShard{epoch}` → refetch the shard map, re-route.
  Data nodes check "do I still own this range at this epoch?" before
  serving a write; that check is what makes stale clients safe.
- **Watch (optional):** proactive push of map changes to subscribed
  clients; the correction path must exist anyway, so add watch only as
  an optimization.

## State machine shape

Same pattern as any raft state machine — commands in, deterministic
state out:

```go
type MetaCmd struct {
    Kind    string // "register", "heartbeat", "shard-update", "op-step", ...
    ReqID   string // client dedup
    Payload []byte
}
```

Rules:

- **Deterministic only.** No wall-clock reads inside apply — liveness
  timeouts are computed by the LEADER before proposing ("mark node 3
  dead"), never inside the state machine. Same for random placement:
  choose in the proposer, record the choice in the command.
- **Validate against current state in apply**, not only at propose
  time: two racing proposals both pass pre-checks; apply-time
  validation (e.g. "op step must be N-1 to move to N") makes the loser
  a no-op instead of a corruption.
- **Epoch bumps are part of the same command** that changes the shard
  table — never a separate write.

## Growing/shrinking the metadata group itself

It is a raft group: learner-first conf changes (etcd-raft.md). Keep it
at 3 or 5 voters on stable nodes; it does not need to scale with data —
its write rate is administrative.

## Failure modes to design for

- Metadata group down → data plane keeps serving from cached maps;
  what stops is REconfiguration (splits, repair, new clients). This
  degradation order is correct — verify it in the harness.
- A node isolated from metadata but not from clients must keep serving
  its shards (epoch checks protect correctness) but refuse operations
  that need fresh placement.
- Never let two ops touch the same shard concurrently: `op/<id>`
  creation must apply-time-fail if the shard is not `state: normal`.
