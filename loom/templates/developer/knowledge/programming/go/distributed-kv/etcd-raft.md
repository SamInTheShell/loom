# etcd raft - a single replication group

`go.etcd.io/raft/v3` is a raft *library*, not a server: it computes what
a correct raft node would do and hands you the results; you own storage,
transport, ticking, and applying. Get the Ready loop contract right and
everything else in this folder builds on it.

```
go get go.etcd.io/raft/v3
```

(Older code imports `go.etcd.io/etcd/raft/v3` - same library, pre-move.)

## The pieces you own

- **Storage** (`raft.Storage` interface): the raft LOG the library
  reads back. Start with `raft.NewMemoryStorage()` plus your own
  persistence; production keeps the log in Pebble (see pebble.md key
  schema) implementing `InitialState / Entries / Term / LastIndex /
  FirstIndex / Snapshot`.
- **Transport**: deliver `raftpb.Message` values to peers, call
  `node.Step(ctx, msg)` on arrival. Any reliable-enough byte channel
  works (wire-protocols.md); messages are safe to drop - raft retries.
- **Tick source**: call `node.Tick()` on a fixed interval (100ms is
  typical). Elections and heartbeats are counted in ticks
  (`ElectionTick: 10`, `HeartbeatTick: 1` → ~1s election timeout).
- **State machine**: apply committed entries to the engine.

## Starting a node

```go
storage := raft.NewMemoryStorage()
c := &raft.Config{
    ID:              uint64(myID),        // nonzero, unique per member
    ElectionTick:    10,
    HeartbeatTick:   1,
    Storage:         storage,
    MaxSizePerMsg:   1 << 20,
    MaxInflightMsgs: 256,
}
// fresh cluster: list all initial members; rejoining node: nil peers
node := raft.StartNode(c, []raft.Peer{{ID: 1}, {ID: 2}, {ID: 3}})
// after restart with persisted state: node = raft.RestartNode(c)
```

## The Ready loop - the contract

This loop IS the node. The order of operations is the correctness
contract; deviating loses data.

```go
ticker := time.NewTicker(100 * time.Millisecond)
for {
    select {
    case <-ticker.C:
        node.Tick()

    case rd := <-node.Ready():
        // 1. PERSIST first: HardState + new log entries must be on
        //    durable storage before anything else observes them.
        wal.Save(rd.HardState, rd.Entries)          // fsync here
        storage.Append(rd.Entries)                   // and into raft.Storage
        // 2. Snapshot from the leader? Persist, then swallow it.
        if !raft.IsEmptySnap(rd.Snapshot) {
            saveSnapshot(rd.Snapshot)                // durable
            storage.ApplySnapshot(rd.Snapshot)
            engine.RestoreFromSnapshot(rd.Snapshot)  // rebuild state machine
        }
        // 3. Send messages to peers - only after persisting above.
        for _, m := range rd.Messages {
            transport.Send(NodeID(m.To), groupID, m)
        }
        // 4. Apply committed entries to the state machine.
        for _, e := range rd.CommittedEntries {
            switch e.Type {
            case raftpb.EntryNormal:
                if len(e.Data) > 0 {
                    engine.ApplyBatch(decode(e.Data), e.Index) // idempotent!
                }
            case raftpb.EntryConfChange:
                var cc raftpb.ConfChange
                cc.Unmarshal(e.Data)
                node.ApplyConfChange(cc)
                applyMembership(cc)                  // update transport peers
            }
            notifyWaiter(e)                          // unblock Propose caller
        }
        // 5. Tell raft this Ready is fully handled.
        node.Advance()

    case m := <-transport.Recv():                    // from peers
        node.Step(ctx, m)
    }
}
```

Hard rules baked into that order:

- **Persist before send** (steps 1→3): a vote or append-ack that isn't
  durable can be retracted by a crash - that is how raft loses data.
- **Apply is idempotent and records `e.Index`**: after a crash you will
  re-apply entries you already applied. The engine stores the applied
  index atomically with the batch (pebble.md) and skips `e.Index <=
  appliedIndex`.
- **Never block the loop.** Applying may not do slow I/O inline if you
  can help it; never call `Propose` from inside the loop (deadlock).

## Proposing writes

```go
func (g *group) Put(ctx context.Context, k, v []byte) error {
    cmd := encode(Op{Put, k, v}, reqID)      // reqID for the waiter map
    ch := g.registerWaiter(reqID)            // resolved in apply step
    if err := g.node.Propose(ctx, cmd); err != nil {
        return err
    }
    select {                                  // wait for commit+apply
    case res := <-ch:
        return res.Err
    case <-ctx.Done():
        g.dropWaiter(reqID)
        return ctx.Err()                      // op may still commit later!
    }
}
```

`Propose` returning nil means "accepted for replication", NOT
"committed". Completion is learned in the apply step. A timed-out
propose may still commit - commands must be idempotent or carry client
request IDs for dedup (wire-protocols.md).

Only the leader makes progress on proposals; followers return a
NotLeader error to the client with `raft.Status().Lead` as the hint.

## Reads

Three options, weakest to strongest:

1. **Stale read** - read local engine anywhere. Cheap; may lag.
2. **Leader lease read** - read on the leader; correct if clocks are
   sane and CheckQuorum is on. Good default.
3. **ReadIndex** - `node.ReadIndex(ctx, token)`; wait until
   appliedIndex ≥ the returned index, then read. Linearizable without
   log writes. Use for anything advertised as strongly consistent.

## Snapshots and log compaction

Unbounded logs sink restarts and disk. Periodically (every N applied
entries):

```go
data := engine.SnapshotBytes()               // or a manifest reference
snap, _ := storage.CreateSnapshot(appliedIndex, &confState, data)
saveSnapshot(snap)
storage.Compact(appliedIndex - keepTrailing) // keep some tail for slow followers
```

For big state machines don't serialize everything into `snap.Data`; put
a small manifest there (e.g. "engine checkpoint at index I, files X")
and ship the bulk out-of-band (`Engine.SnapshotForRange`), exactly like
shard moves do in sharding.md.

## Membership changes

One change at a time, always through the log:

```go
cc := raftpb.ConfChange{Type: raftpb.ConfChangeAddLearnerNode, NodeID: 4}
node.ProposeConfChange(ctx, cc)
// learner catches up via snapshot+log, then:
//   ConfChangeAddNode 4        (promote to voter)
//   ConfChangeRemoveNode 1     (retire old member)
```

Add as **learner first**, promote when caught up - adding a cold voter
shrinks effective quorum until it syncs. Never remove the node you are
currently talking to as leader without transferring leadership first
(`node.TransferLeadership`).

## Config knobs that matter

- `CheckQuorum: true` and `PreVote: true` - both on for real
  deployments: they prevent stale leaders and rejoin-storm elections.
- `MaxInflightMsgs`/`MaxSizePerMsg` - replication pipelining; defaults
  fine to start.
- Tick interval trades failover speed vs false elections; 100ms tick
  with ElectionTick 10 ≈ 1-2s failover.

## Testing (see ../testing.md)

Drive `Tick()` manually - an election is exactly ElectionTick+1 ticks
away, deterministic. Fake the transport with channels; partition = stop
delivering between sets. The harness invariant after every scenario:
every node's applied log prefix is identical, and every acked write is
in it.
