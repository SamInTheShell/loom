# Erasure coding - Reed-Solomon for blob durability

Erasure coding stores a blob chunk as `data + parity` shards on
different nodes so that ANY `data` of them reconstruct the original.
Compared to 3× replication, a 4+2 scheme survives the same two failures
at 1.5× storage instead of 3×. The cost: reads need multiple nodes,
repairs need computation, and small objects don't amortize the
overhead - which is why the databox design replicates small chunks and
erasure-codes large ones (blob-storage.md decides; this file is the
mechanism).

Library: `github.com/klauspost/reedsolomon` (SIMD-accelerated, the
de-facto standard).

```
go get github.com/klauspost/reedsolomon
```

## Encoding

```go
const dataShards, parityShards = 4, 2

enc, err := reedsolomon.New(dataShards, parityShards)

// Split pads and slices the input into dataShards equal pieces
shards, err := enc.Split(chunk)          // [][]byte, len = 4, equal size
// grow the slice with parity buffers and fill them
shards = append(shards, make([]byte, len(shards[0])), make([]byte, len(shards[0])))
err = enc.Encode(shards)                 // computes parity in place

ok, err := enc.Verify(shards)            // true if parity consistent
```

Store each shard on a DIFFERENT node (placement in blob-storage.md).
Record in the chunk's metadata: scheme (4+2), shard size, original
chunk length (Split pads - you need the true length to trim after
join), and a checksum of the ORIGINAL chunk.

## Reconstruction

```go
// fetch what you can; nil marks a missing shard
shards := [][]byte{s0, nil, s2, s3, nil, s5}   // any ≥4 present works

err = enc.Reconstruct(shards)            // rebuilds ALL missing shards
// or, cheaper when you only need the payload back:
err = enc.ReconstructData(shards)        // rebuilds only data shards

var buf bytes.Buffer
err = enc.Join(&buf, shards, originalLen) // trim padding to true length
```

With more than `parityShards` missing, `Reconstruct` returns
`reedsolomon.ErrTooFewShards` - that chunk is lost; surface it loudly
(scrub report, admin UI), never silently.

## Read path strategy

1. Try the `dataShards` nodes holding data shards - if all answer, the
   payload is a straight concatenation (Join with no reconstruction;
   zero decode cost).
2. On timeout/miss, fetch parity shards from remaining nodes as
   replacements and `ReconstructData`.
3. Optional latency trick: request `data + 1` shards up front and use
   the first `data` to arrive ("hedged reads") - pay one extra fetch
   for tail-latency immunity.

Always verify the ORIGINAL chunk checksum after join/reconstruct -
Reed-Solomon detects nothing by itself; it only recomputes. The
checksum is the integrity truth (blob-storage.md makes it the chunk's
name anyway: content addressing).

## Repair (scrub integration)

When a node dies or a shard fails its checksum (blob-storage.md's
scrub loop finds both):

1. Fetch any `dataShards` healthy shards of the affected chunk.
2. `Reconstruct`, keep the shard(s) that belong on the replacement
   node, write them there, update chunk metadata.
3. Rate-limit repairs cluster-wide: a dead 10 TB node means mass
   reconstruction traffic; an unthrottled repair storm is its own
   outage. Prioritize chunks at `parity` remaining (one more loss =
   data loss) over chunks merely one shard short.

## Choosing a scheme

- `4+2` - good default: 1.5× overhead, survives 2 losses, needs 6
  placement nodes.
- `8+3` - 1.375×, survives 3, needs 11 nodes; only when the cluster is
  comfortably larger than 11 and blobs are big.
- Below ~1 MB per chunk, shard overhead beats the savings - REPLICATE
  small chunks (3×) instead; record the choice per chunk. A mixed
  fleet needs both paths anyway (replication is also the bootstrap
  implementation in build-plan.md M8).
- The scheme is per-chunk metadata, not a global constant - clusters
  migrate schemes chunk-by-chunk during scrub rewrites.

## Rules

- Never place two shards of one chunk on one node (or one failure
  domain if you track racks) - placement must enforce it, verify in
  the harness.
- Shard buffers are exactly `len(chunk)/dataShards` rounded up; all
  equal length - `Split` handles it, hand-built buffers must match or
  Encode errors.
- `reedsolomon.New` instances are safe for concurrent Encode on
  DIFFERENT shard sets; reuse one per scheme (construction builds
  tables).
- Fuzz the metadata decode and property-test round-trips: random
  chunk → split/encode → knock out ≤ parity shards at random →
  reconstruct → byte-equal (../testing.md).
