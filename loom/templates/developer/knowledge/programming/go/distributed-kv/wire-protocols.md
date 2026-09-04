# Wire protocols

The three kinds of traffic in a databox-style system, and how to put
each on the wire. The advice up front: **use gRPC for node-to-node and
client RPC unless you have a concrete reason not to; if you do build a
custom TCP protocol, use the length-prefixed frame below and nothing
cleverer.** Both share the same rules at the bottom.

Traffic classes (they have different needs - it is fine to mix
solutions):

1. **Raft transport** - many small messages, node↔node, loss-tolerant
   (raft retries), needs batching (multigroup-raft.md).
2. **Client RPC** - request/response + streaming for scans and blobs,
   needs auth, deadlines, and good errors (user-systems.md,
   sharding.md's routing errors).
3. **Bulk transfer** - snapshots and chunk shards; big sequential
   streams where throughput dominates.

## Option A: gRPC (`google.golang.org/grpc`)

You get HTTP/2 framing, streaming, deadlines, TLS, and generated types
for free; the cost is proto tooling and dependency weight.

```proto
service KV {
  rpc Get(GetReq) returns (GetResp);
  rpc Put(PutReq) returns (PutResp);
  rpc Scan(ScanReq) returns (stream ScanResp);      // paged results
  rpc Raft(stream RaftEnvelope) returns (stream RaftEnvelope); // long-lived pipe
  rpc PutShard(stream ShardChunk) returns (ShardAck);          // bulk
}
message RaftEnvelope { uint64 group = 1; bytes msg = 2; }      // raftpb bytes
```

- Per-request metadata (token, shard epoch) rides gRPC metadata;
  deadlines ride `context.Context` natively.
- Errors: `status.New(codes.FailedPrecondition, "wrong shard").
  WithDetails(&WrongShard{Epoch: e})` - typed details, not string
  parsing, for the NotLeader/WrongShard routing contract.
- Raft over a long-lived bidirectional stream per node pair; batch
  envelopes before Send (the multigroup batching point).
- Keep the proto package versioned (`databox.v1`) from day one.

## Option B: custom TCP with length-prefixed frames

For a dependency-light system (or the learning value). One frame
format, everywhere:

```
frame := len(4B big-endian, payload only) | type(1B) | payload
```

```go
func WriteFrame(w *bufio.Writer, typ byte, payload []byte) error {
    var hdr [5]byte
    binary.BigEndian.PutUint32(hdr[:4], uint32(len(payload))+1)
    hdr[4] = typ
    if _, err := w.Write(hdr[:]); err != nil { return err }
    _, err := w.Write(payload)
    return err
}

func ReadFrame(r *bufio.Reader, maxFrame uint32) (byte, []byte, error) {
    var hdr [4]byte
    if _, err := io.ReadFull(r, hdr[:]); err != nil { return 0, nil, err }
    n := binary.BigEndian.Uint32(hdr[:])
    if n == 0 || n > maxFrame {                     // 16 MiB is a sane cap
        return 0, nil, fmt.Errorf("frame size %d out of range", n)
    }
    buf := make([]byte, n)
    if _, err := io.ReadFull(r, buf); err != nil { return 0, nil, err }
    return buf[0], buf[1:], nil
}
```

Non-negotiables for a custom protocol:

- **Cap the frame size and validate BEFORE allocating** - the code
  above allocates after the check; a missing cap is a one-packet OOM.
- **Fuzz the decoder from the day it exists** (../testing.md): frames,
  then payload decoding. `io.ReadFull` everywhere; never `Read` once
  and hope.
- **Connection hello**: first frame carries protocol version + node
  ID + cluster ID. Refuse mismatched cluster IDs loudly (protects
  against cross-cluster config mistakes) and too-new versions politely
  (min/max version negotiation → rolling upgrades work).
- **Payload encoding**: protobuf even on custom framing (stable,
  versioned, fast); `encoding/gob` is Go-only and version-brittle;
  hand-rolled structs are where corruption bugs live. JSON is fine for
  the admin API only.
- **Request framing**: every request carries a uint64 request ID; the
  response echoes it - that is what allows pipelining and per-request
  deadlines over one connection.
- One goroutine reading, one writing per conn; writers feed a channel
  (this is also where raft message batching happens). Deadlines via
  `SetReadDeadline` around `ReadFrame`, keepalive pings at the frame
  layer.

## Rules for either option

- **Idempotence at the protocol level**: mutating requests carry a
  client ID + sequence number; the state machine remembers the last
  seq per client (in the replicated state!) and returns the recorded
  answer on replay. This is what makes "timeout → retry" safe
  (etcd-raft.md's "propose may commit after ctx expires").
- **Deadlines are mandatory**: every RPC takes a context; servers
  check `ctx.Err()` before expensive work; no infinite waits anywhere.
- **Routing errors are data**: NotLeader{leaderHint} and
  WrongShard{epoch} are structured responses (sharding.md client
  rules), never opaque failures.
- **Checksum bulk data end-to-end** (blob shards carry their hash -
  blob-storage.md); TCP's checksum is not integrity.
- **TLS**: terminate with `crypto/tls` on both options; node certs for
  inter-node (mutual TLS), server certs for clients. Wire it in at M2
  of the build plan while there is one listener, not at M9 when there
  are five.
- Version every payload schema; never re-number proto fields; unknown
  fields are ignored not errors - rolling upgrades depend on all
  three.
