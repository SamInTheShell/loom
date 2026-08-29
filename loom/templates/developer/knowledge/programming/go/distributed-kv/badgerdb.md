# BadgerDB — alternative storage engine

`github.com/dgraph-io/badger/v4` is the other production-grade pure-Go
KV engine. Same job as Pebble (pebble.md) behind the same `Engine`
seam; different architecture with different trade-offs. Support it as a
swappable engine — implementing the seam twice also proves the seam.

```
go get github.com/dgraph-io/badger/v4
```

## How it differs from Pebble (choose with this table)

| | Pebble | Badger |
| --- | --- | --- |
| Design | classic LSM (keys+values together) | WiscKey: keys in LSM, values ≥ threshold in a value log |
| Large values | fine, but compaction rewrites them | excellent — values never rewritten by compaction |
| Ordered scans | fast (values inline) | slower for big values (pointer chase to vlog) |
| Transactions | write batches only | real read-write transactions (SSI) |
| Space reclaim | automatic compaction | compaction + MANUAL value-log GC you must run |
| TTL per key | no (build in key schema) | built in (`WithTTL`) |

Rules of thumb for databox: Pebble for the KV/raft data (scan-heavy,
small values, no GC chore); Badger shines for a chunk/blob store
(large values, point lookups by hash — blob-storage.md) — though a
plain file-per-chunk layout is also legitimate there.

## Core API — everything is a transaction

```go
db, err := badger.Open(badger.DefaultOptions(dir))
defer db.Close()

// read-only txn
err = db.View(func(txn *badger.Txn) error {
    item, err := txn.Get([]byte("k"))      // badger.ErrKeyNotFound
    if err != nil { return err }
    return item.Value(func(val []byte) error {
        v = append([]byte(nil), val...)    // copy out of the callback
        return nil
    })
})

// read-write txn — atomic, may return badger.ErrConflict under SSI
err = db.Update(func(txn *badger.Txn) error {
    if err := txn.Set([]byte("k1"), v1); err != nil { return err }
    return txn.Delete([]byte("k2"))
})

// prefix iteration (keys are byte-ordered like pebble)
db.View(func(txn *badger.Txn) error {
    opts := badger.DefaultIteratorOptions
    opts.Prefix = []byte("d/42/")
    it := txn.NewIterator(opts)
    defer it.Close()
    for it.Rewind(); it.Valid(); it.Next() {
        item := it.Item()
        _ = item.Value(func(v []byte) error { use(item.Key(), v); return nil })
    }
    return nil
})

// TTL
e := badger.NewEntry(k, v).WithTTL(time.Hour)
db.Update(func(txn *badger.Txn) error { return txn.SetEntry(e) })
```

Big transactions overflow (`ErrTxnTooBig`): for bulk loads use
`db.NewWriteBatch()` (no read-your-writes, auto-splits internally).

## Using it behind the Engine seam

- The raft apply loop needs only `Update` with the applied-index write
  in the same transaction — the identical idempotence pattern as
  pebble.md, expressed as one `db.Update`.
- `ErrConflict` cannot happen in the apply path (single writer), but
  handle it generically anyway (retry) so the engine wrapper is honest.
- Map `badger.ErrKeyNotFound` → the Engine's ErrNotFound at the
  boundary.
- Iterators live inside a `View`; the Engine's `NewIter` wrapper must
  therefore either collect bounded pages per call or hold a txn open —
  prefer paged collection (bounded memory, no long-lived txn pins).

## Value-log GC — the operational chore Badger adds

Deleting/overwriting values does not reclaim vlog space until you run
GC. Run it periodically from a background goroutine:

```go
ticker := time.NewTicker(10 * time.Minute)
for range ticker.C {
again:
    err := db.RunValueLogGC(0.5)   // rewrite files ≥50% garbage
    if err == nil { goto again }   // one file per call — repeat until
    // badger.ErrNoRewrite => nothing worth collecting; stop
}
```

Skip this and disk usage grows without bound on churn-heavy workloads.
It is the price of never rewriting values during compaction.

## Options that matter

```go
opts := badger.DefaultOptions(dir)
opts.ValueThreshold = 1 << 10   // values under 1KB stay in the LSM —
                                 // raise for blob use, lower for KV use
opts.NumVersionsToKeep = 1
opts.Logger = nil                // silence its chatty default logger
db, err := badger.Open(opts)
```

`ValueThreshold` is the knob that decides which architecture you
actually get — set it consciously per use case.

## Gotchas

- Value bytes are ONLY valid inside the `item.Value(func...)` callback
  (or use `item.ValueCopy(nil)`). Same copy-discipline as pebble.
- One process per DB dir (file lock), `t.TempDir()` per test.
- Crash recovery replays the vlog on open; opens on big DBs can take
  seconds — fine for servers, surprising in tests.
- `db.Sync()` exists but individual `Update`s are durable by default
  (`SyncWrites` option trades this for speed — if you turn it off, the
  raft log had better be elsewhere, exactly like the pebble fsync
  note).
