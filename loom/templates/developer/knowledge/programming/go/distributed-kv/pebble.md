# Pebble - the storage engine

`github.com/cockroachdb/pebble` is a pure-Go LSM key-value engine
(RocksDB-compatible design, built for CockroachDB). It is the
recommended engine for the databox design: ordered iteration, range
deletes, batches with a shared WAL, and SST ingestion map exactly onto
what raft groups and shard moves need. (Alternative: badgerdb.md.)

```
go get github.com/cockroachdb/pebble
```

## Core API

```go
db, err := pebble.Open(dir, &pebble.Options{})
defer db.Close()

// writes - Sync waits for the WAL fsync; NoSync rides a later one
err = db.Set([]byte("k"), []byte("v"), pebble.Sync)
err = db.Delete([]byte("k"), pebble.Sync)

// reads - value is only valid until closer.Close()
value, closer, err := db.Get([]byte("k"))   // err == pebble.ErrNotFound
if err == nil {
    v := append([]byte(nil), value...)      // copy out, then
    closer.Close()
}

// atomic multi-op writes
b := db.NewBatch()
b.Set(k1, v1, nil)
b.Delete(k2, nil)
b.DeleteRange(lo, hi, nil)                  // tombstone a whole range
err = db.Apply(b, pebble.Sync)              // all-or-nothing

// ordered iteration, half-open [lo, hi)
it, _ := db.NewIter(&pebble.IterOptions{LowerBound: lo, UpperBound: hi})
for it.First(); it.Valid(); it.Next() {
    use(it.Key(), it.Value())               // valid only until Next/Close
}
it.Close()
```

Keys and values are plain `[]byte`, ordered bytewise. There are no
column families and no transactions with reads - batches are write-only
atomicity, which is exactly enough for a raft apply loop (reads never
need to be transactional with writes because ALL writes come from the
single apply thread - the Engine contract in this folder's README).

## Key schema - one DB, many namespaces

Byte-ordered keys make prefixes into namespaces. The databox layout
(shared by multigroup-raft.md):

```
a/<group>                 applied raft index (8-byte big-endian)
r/<group>/e/<index>       raft log entry        (index big-endian!)
r/<group>/h               raft HardState
r/<group>/desc            group descriptor
d/<group>/<userkey>       user data
c/<chunkhash>             blob chunks (blob-storage.md) - often a
                          separate pebble instance; see below
```

Rules: fixed-width big-endian for anything numeric that must sort
numerically; a separator byte (`/` works if user keys are escaped, or
use 0x00) between prefix components; never concatenate variable-length
parts without one.

## The applied-index trick (idempotent replay)

The single most important pattern for a raft state machine - apply the
batch AND record how far you applied, atomically:

```go
func (e *Eng) ApplyBatch(ops []Op, group GroupID, index uint64) error {
    if index <= e.appliedIndex(group) {
        return nil                       // replay after restart: skip
    }
    b := e.db.NewBatch()
    for _, op := range ops { op.addTo(b, group) }
    b.Set(appliedKey(group), be64(index), nil)   // same batch!
    return e.db.Apply(b, pebble.NoSync)          // see fsync note
}
```

Fsync note: the raft LOG must be `Sync` (it is the durability raft
promises). The APPLY batch may be `NoSync` - after a crash, replay from
the durable log re-derives it. This is a large write-amplification win.
If log and state share one pebble DB, the log write's Sync covers the
WAL anyway; keep the reasoning explicit in a comment.

## Snapshots and shard moves

- Point-in-time reads: `snap := db.NewSnapshot()` → `snap.Get`,
  `snap.NewIter` see a frozen view while writes continue. Use for
  `Engine.SnapshotForRange` (stream a shard's `d/<group>/` range to a
  learner - sharding.md).
- Receiving a snapshot: write the incoming stream into SSTs with
  `sstable.NewWriter` and hand them to `db.Ingest([]string{...})` -
  files drop into the LSM wholesale, no per-key write path. Fall back
  to plain batched Sets if ingestion is fiddly; correctness first
  (build-plan.md rule: optimize later).
- Dropping data (shard moved away, group destroyed):
  `DeleteRange` + `db.Compact(lo, hi, false)` to reclaim space
  promptly.

## Options that matter (start small)

```go
&pebble.Options{
    // defaults are sane; revisit when metrics say so:
    MemTableSize:                64 << 20,
    MaxConcurrentCompactions:    func() int { return 2 },
    L0CompactionThreshold:       4,
}
```

- `db.Metrics()` returns a rich struct - expose it on the admin
  frontend (frontends.md) from day one; LSM problems (L0 pileup,
  compaction debt) are visible there long before they hurt.
- One pebble instance per node is the default. A SECOND instance for
  blob chunks is reasonable: chunk traffic is large-value sequential
  and would churn the KV's cache; separate Options tune each.

## Gotchas

- `Get` values and iterator Key/Value are only valid until
  Close/Next - copy if kept. The #1 pebble bug in new code.
- `pebble.ErrNotFound` is the sentinel - map it to the Engine's
  ErrNotFound at the boundary, don't leak pebble types upward.
- Iterators pin memtables/SSTs: long-lived iterators block reclaim.
  Scan in bounded chunks and re-seek.
- Open is exclusive (file lock) - a second Open on the same dir fails;
  tests must use `t.TempDir()` per instance.
- Reopen after crash replays the WAL automatically; your job is only
  the applied-index skip shown above.
