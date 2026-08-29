# Multi-group raft — many groups in one process

A sharded store runs one raft group per shard (see sharding.md), so a
node hosts replicas of MANY groups. This file is the delta from a single
group (etcd-raft.md) to hundreds per process. The single-group contract
is unchanged — this is plumbing to run N of them affordably.

## What is shared, what is per-group

Per group: a `raft.Node`, its `raft.Storage` log view, its applied
index, its Ready-loop goroutine (or scheduler slot).

Shared across all groups on the node:

- **One ticker.** Do NOT create a time.Ticker per group. One 100ms
  ticker fans out `Tick()` to every group (a slice walk; raft ticks are
  cheap). Thousands of timers is scheduler noise for nothing.
- **One transport.** Every message is addressed `(to NodeID, GroupID,
  raftpb.Message)`; the receiver demuxes to the right group's `Step`.
  Batch messages per destination node per flush — heartbeats for 500
  groups to the same peer should ride one network write, not 500.
- **One storage engine.** All groups log into one Pebble DB under
  per-group prefixes (pebble.md):

  ```
  r/<group>/e/<index>   raft log entry
  r/<group>/h           HardState
  r/<group>/s           snapshot manifest
  a/<group>             applied index
  d/<group>/<userkey>   state machine data
  ```

  One shared engine means one WAL and batched fsyncs across groups —
  the difference between 100 and 10k fsyncs/s under load.

## The node skeleton

```go
type Node struct {
    id     NodeID
    groups map[GroupID]*Group   // guarded; groups come and go (splits!)
    eng    Engine               // shared pebble
    tr     Transport
}

func (n *Node) run() {
    ticker := time.NewTicker(100 * time.Millisecond)
    for {
        select {
        case <-ticker.C:
            for _, g := range n.snapshotGroups() { g.raft.Tick() }
        case env := <-n.tr.Recv():              // (GroupID, Message)
            g := n.group(env.Group)
            if g == nil {
                n.maybeCreateReplica(env)       // see below
                continue
            }
            g.raft.Step(ctx, env.Msg)
        }
    }
}
```

Each group still runs its own Ready-drain (goroutine per group is fine
into the low thousands; beyond that, a worker pool draining a queue of
"groups with pending Ready" — the contract per group is identical).

**Batched persistence:** collect the HardState+Entries of every group
that produced a Ready this cycle into ONE engine batch, one fsync, then
send all messages, then apply all committed entries. This is the big
multi-group win; the per-group ordering rules (persist → send → apply →
Advance) still hold within each group.

## Group lifecycle

Groups are created and destroyed at runtime — this is what makes splits
and rebalancing (sharding.md) possible.

- **Create** (bootstrap or split): write a group descriptor
  (`r/<group>/desc`: members, key range) in the same batch that creates
  the log state, then `raft.StartNode` (initial members) or
  `RestartNode` (rejoin after restart). On node restart, scan
  `r/*/desc` and restart every local replica.
- **Create on demand**: a raft message for an unknown group may mean
  the metadata group placed a new replica here (a move in progress).
  Only create a replica in response to a message if the metadata group's
  placement says this node should host it — otherwise drop the message.
  (Unconditional create-on-message resurrects deleted groups.)
- **Destroy** (after a move away or merge): stop the goroutine, delete
  `r/<group>/*`, `a/<group>`, `d/<group>/*` with a range delete, and
  record a tombstone (`t/<group>` with a TTL) so late messages for the
  dead group are ignored instead of re-creating it.

## Leadership spread and quiescence

- Many groups = many leaders. Spread them: if a node leads far more
  than `total_leaders / nodes`, use `TransferLeadership` on some
  groups. The metadata group is a fine place to run this balancer.
- Idle groups still heartbeat. At hundreds of groups either accept the
  (small, batched) cost, or implement quiescence: stop ticking groups
  with no traffic and wake them on the first message/proposal. Do
  quiescence LAST — it is an optimization with real edge cases, not a
  requirement (this is also rung M6 vs later in build-plan.md).

## Rules

- The per-group Ready contract from etcd-raft.md is unchanged; never
  interleave one group's persist/send/apply steps out of order even
  when batching across groups (persist ALL, then send ALL, is fine —
  it strengthens the ordering, never weakens it).
- `raft.Config.ID` is the NODE id and stays the same for every group on
  the node; the pair (GroupID, NodeID) names a replica.
- Never share an applied-index key between groups; replay after restart
  is per-group.
- Test with the same in-process harness (../testing.md): the transport
  fake gains a GroupID field, and invariants run per group.
