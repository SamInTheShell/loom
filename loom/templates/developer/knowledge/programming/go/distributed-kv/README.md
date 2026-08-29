# Distributed KV + blob store — the assembly map

This folder is a component catalog for building a databox-style
distributed key-value and blob store in Go. Each file covers one part
with concrete APIs and contracts; this README is the map that shows how
they compose, so the parts can be assembled into different system
configurations without guessing.

## The layer diagram

```
clients / admin UI
      │
frontends.md          HTTP + templates (admin), client API
wire-protocols.md     gRPC or length-prefixed TCP between all nodes
      │
user-systems.md       accounts, tokens, authorization
      │
metadata-group.md     ONE raft group holding cluster truth:
                      node registry, shard table, config epochs
      │            ┌──────────────────────────────────────────┐
sharding.md        │ many DATA shards: shard = key range =    │
multigroup-raft.md │ one raft group over N replicas           │
etcd-raft.md       │                                          │
      │            └──────────────────────────────────────────┘
pebble.md /           each replica applies its raft log to a
badgerdb.md           local storage engine
      │
blob-storage.md       large values: chunked, content-addressed,
erasure-coding.md     erasure-coded across nodes; KV holds manifests
```

## Configurations you can assemble

Pick the smallest configuration that meets the need; every larger one
contains the smaller ones unchanged.

1. **Single-node KV** — pebble.md (or badgerdb.md) + wire-protocols.md
   + frontends.md. No raft, no shards. The storage API you define here
   is reused verbatim by every later configuration.
2. **Replicated KV (one shard)** — add etcd-raft.md: one raft group,
   the state machine applies to the storage engine. The "cluster" and
   the "metadata group" are the same single group.
3. **Sharded KV** — add metadata-group.md, multigroup-raft.md,
   sharding.md: a dedicated metadata group routes clients to many data
   groups; shards split and move.
4. **KV + blob store** — add blob-storage.md + erasure-coding.md: the
   KV (any of the above) stores blob manifests; blob chunks live on a
   separate flat chunk store across nodes.

## Reading order for building (see build-plan.md for the full ladder)

1. `../testing.md` — the harness patterns everything below depends on.
2. `build-plan.md` — how to sequence the work and verify each step.
3. `pebble.md` (or `badgerdb.md`) — the storage engine and key schema.
4. `etcd-raft.md` — the Ready loop; the heart of replication.
5. `metadata-group.md`, `multigroup-raft.md`, `sharding.md` — scale-out.
6. `wire-protocols.md`, `user-systems.md`, `frontends.md` — the edges.
7. `blob-storage.md`, `erasure-coding.md` — large data.

## Contracts that keep the parts composable

These interfaces are the seams between files. Keep them stable and every
component can be swapped or tested alone.

```go
// Storage engine seam (pebble.md, badgerdb.md implement this)
type Engine interface {
    Get(key []byte) ([]byte, error)          // ErrNotFound when absent
    ApplyBatch(b []Op, appliedIndex uint64) error // atomic, idempotent
    NewIter(lo, hi []byte) Iter
    SnapshotForRange(lo, hi []byte) (io.ReadCloser, error)
    Close() error
}

// Replication seam (etcd-raft.md / multigroup-raft.md implement this)
type Group interface {
    Propose(ctx context.Context, cmd []byte) error // returns after commit+apply
    IsLeader() bool
    LeaderHint() NodeID
}

// Transport seam (wire-protocols.md implements; tests fake it)
type Transport interface {
    Send(to NodeID, group GroupID, msg raftpb.Message)
}
```

Rules that make the composition work:

- **All writes go through raft.** Nothing writes to the engine except
  the apply loop. Reads may be served locally (see etcd-raft.md for
  read safety options).
- **The metadata group is the only source of cluster truth.** Data
  nodes and clients cache it and are corrected via epoch checks
  (sharding.md); nobody else invents placement.
- **Every apply is idempotent** and carries its raft index so replay
  after restart is harmless (pebble.md shows the applied-index batch
  trick).
- **Errors route clients, not humans**: NotLeader carries a leader
  hint, WrongShard carries the new shard map epoch. Clients self-heal.

## Siblings

- `../git-hosting/` — a complete git forge whose storage backend is
  the KV + blob store this folder builds.
- `../urfave-cli-v3.md` — the CLI shell for the server/ops/client
  commands frontends.md describes.
- `../../web/node-graph.md` — an interactive SVG node-graph for
  visualizing the cluster topology this folder builds.
