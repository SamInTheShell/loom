# Managing blob data

Blobs (values from megabytes to terabytes) do not belong in raft
groups — replicating bulk bytes through a consensus log triples write
cost and wrecks compaction. The databox design stores blob DATA on a
flat chunk store across nodes and keeps only small MANIFESTS in the
replicated KV. The KV brings the consistency; the chunk store brings
the capacity.

## The shape

```
PUT blob ──► chunker ──► chunks (content-addressed by SHA-256)
                              │ each chunk: replicate 3× (small)
                              │ or erasure-code 4+2 (large) ──► chunk servers
                              ▼
             manifest { chunks: [hash, len, placement...], size, checksum }
                              ▼
             KV (raft-replicated): b/<blobID> = manifest
```

- **Chunking**: fixed-size (e.g. 8 MiB) is simple and right for a
  first system. Content-defined chunking (rolling hash) adds dedup for
  overlapping data — a later optimization, same manifest shape.
- **Content addressing**: a chunk's name IS `sha256(bytes)`. Integrity
  check = recompute and compare; dedup = same hash, same chunk, bump
  refcount. Chunks are immutable — blob updates write new chunks and a
  new manifest version.
- **Placement**: the metadata group assigns each chunk's shard/replica
  nodes (metadata-group.md `place()`), recorded in the manifest.
  Consistent hashing over the node ring is a fine stateless
  alternative — but recorded placement makes repair and rebalance far
  easier to reason about; prefer it.

## Write path

1. Client streams the blob to any node (or a coordinator it's told to
   use — frontends.md API).
2. Coordinator chunks the stream; per chunk: hash → ask metadata (or
   its cached policy) where it lives → if the hash already exists,
   skip upload (dedup) → else write shards/replicas to chunk servers,
   each of which fsyncs and acks with the hash it verified.
3. All chunks durable → commit the manifest to the KV with a `pending`
   → `live` flip in one KV write.
4. Ack the client only after the manifest commit — the manifest is the
   truth; chunks without a live manifest are garbage (collectible),
   never the reverse.

Failure anywhere before step 3 leaves only orphan chunks — harmless,
GC's job. This ordering (data first, then metadata, then ack) is the
whole crash-consistency story; never reorder it.

## Read path

Manifest from the KV → fetch chunks (parallel, bounded fanout) →
verify each chunk's hash → stream to client in order. Range reads seek
straight to the covering chunks (`offset / chunkSize`). Erasure-coded
chunks follow the read strategy in erasure-coding.md.

## Chunk servers

Deliberately dumb: store shard bytes by hash, verify on write and
read, serve them back. Implementation: files in a fanout dir tree
(`chunks/ab/cd/<hash>`, fsync file then parent dir) or a Badger
instance with a high ValueThreshold (badgerdb.md). Endpoints:
`PutShard(hash, idx, bytes)`, `GetShard(hash, idx, [range])`,
`Has(hash, idx)`, `Delete(hash, idx)`, `ListPrefix(p)` (for scrub
inventory). No cluster logic lives here — placement, repair and GC are
driven from above; that split keeps chunk servers trivially testable.

## Garbage collection

Refcounting alone breaks on crashes (orphans from failed writes never
got counted). Use mark-sweep with a grace period, driven by a metadata
op so it resumes after coordinator crashes (metadata-group.md):

1. **Mark**: iterate all live manifests in the KV, build/refresh the
   reachable-chunk set (a bloom filter or a KV table `gc/<hash>` —
   must not need to fit in RAM).
2. **Sweep**: each chunk server lists its chunks; anything not
   reachable AND older than the grace period (e.g. 24h, covering the
   longest plausible in-flight upload) is deleted.
3. Uploads in progress are protected by age, not bookkeeping — that is
   what the grace period is FOR; do not shrink it below upload
   timeouts.

Property test (build-plan.md M8 gate): random interleaving of
uploads/deletes/GC never collects a chunk reachable from any live
manifest.

## Scrub and repair

A background loop, throttled, forever:

- Each chunk server walks its store, re-hashing a slice per cycle;
  corrupt/missing shards are reported to the repair queue.
- Node death (metadata liveness) enqueues every shard placed on it.
- Repair executes per erasure-coding.md (or re-replication), updates
  manifests/placement, and is rate-limited with loss-proximity
  priority.

Scrub cadence: full pass every 1–2 weeks is a sane target; expose the
queue depth and last-pass age on the admin UI (frontends.md) — a
stalled scrub is silent durability decay.

## Rules

- The KV manifest is the single source of truth; chunk existence
  proves nothing (orphans are normal).
- Chunks are immutable and content-addressed; there is no update path.
- Every byte crossing a node boundary is verified against its hash on
  arrival — corruption is caught at write time, not read time.
- Deletion is manifest deletion; bytes die later via GC. "Delete"
  latency and space reclaim are separate metrics — say so in the UI.
