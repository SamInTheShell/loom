# Reliable testing in Go

How to write Go tests that fail only when the code is wrong - never
because of timing, ordering, or leftover state. For distributed systems
this is the foundation: an untested replication protocol is a rumor.

## The baseline rules

1. Run everything with the race detector: `go test -race ./...`. A data
   race found in CI is a bug found for free; one found in production is
   an outage. Make `-race` the default in CI, not an option.
2. No `time.Sleep` to "wait for things to settle". Sleeping makes tests
   slow AND flaky. Wait for a condition instead (see `waitFor` below) or
   remove real time entirely (injected clock).
3. Every test gets fresh state: `t.TempDir()` for directories (auto
   cleaned), fresh structs, fresh ports (`:0` and read the bound addr).
   Tests must pass with `go test -count=2 -shuffle=on`.
4. Table-driven tests with subtests are the default shape:

   ```go
   func TestParseKey(t *testing.T) {
       tests := []struct {
           name    string
           in      string
           want    Key
           wantErr bool
       }{
           {"simple", "a/b", Key{"a", "b"}, false},
           {"empty", "", Key{}, true},
           {"unicode", "α/β", Key{"α", "β"}, false},
       }
       for _, tt := range tests {
           t.Run(tt.name, func(t *testing.T) {
               got, err := ParseKey(tt.in)
               if (err != nil) != tt.wantErr {
                   t.Fatalf("err = %v, wantErr %v", err, tt.wantErr)
               }
               if !tt.wantErr && got != tt.want {
                   t.Errorf("got %v, want %v", got, tt.want)
               }
           })
       }
   }
   ```

## Waiting on asynchronous conditions

When real concurrency is unavoidable, poll the condition with a deadline
- never a bare sleep:

```go
func waitFor(t *testing.T, timeout time.Duration, cond func() bool, msg string) {
    t.Helper()
    deadline := time.Now().Add(timeout)
    for time.Now().Before(deadline) {
        if cond() {
            return
        }
        time.Sleep(5 * time.Millisecond)
    }
    t.Fatalf("timed out waiting for %s", msg)
}

waitFor(t, 5*time.Second, func() bool {
    return cluster.LeaderID() != 0
}, "leader election")
```

## Injected clocks - remove real time from the code under test

Code that calls `time.Now()` or `time.After` directly cannot be tested
deterministically. Inject a clock interface; production passes the real
one, tests pass a fake they advance by hand:

```go
type Clock interface {
    Now() time.Time
    NewTicker(d time.Duration) *time.Ticker // or a Ticker interface
}
```

For raft-style code, drive ticks explicitly in tests: call `node.Tick()`
N times to force an election instead of waiting real milliseconds. This
is the single biggest de-flaker for consensus tests - see
`distributed-kv/etcd-raft.md`, whose loop is built around an explicit
Tick for exactly this reason.

## In-process cluster harness

Test a distributed system as N nodes in ONE process wired by an
in-memory transport - no real network, no containers, milliseconds per
test. The pattern:

1. Define the transport as an interface in production code
   (`Send(to NodeID, msg Message)`); the real implementation does TCP or
   gRPC, the test implementation delivers through channels.
2. Give the test transport failure knobs: `Partition(a, b)`,
   `Drop(fraction)`, `Delay(d)`, `Reorder()`. Chaos becomes a test input
   instead of a production surprise.
3. Build a `TestCluster` helper: start N nodes, expose
   `Leader()`, `Restart(i)`, `Partition(...)`, and a client. Every
   protocol test then reads as a scenario:

   ```go
   c := NewTestCluster(t, 3)
   c.MustPut("k", "v1")
   c.Partition(c.LeaderIdx())          // old leader isolated
   waitFor(t, 5*time.Second, c.HasNewLeader, "re-election")
   c.MustPut("k", "v2")               // majority side still works
   c.Heal()
   c.WaitConverged(t)                  // isolated node catches up
   if got := c.MustGet("k"); got != "v2" { t.Fatal(got) }
   ```

4. After every scenario, verify INVARIANTS, not just the last read:
   all nodes applied the same log prefix, no committed write lost, one
   leader per term. Invariant checks catch bugs scenarios miss.

## Goroutine and resource leaks

Add `go.uber.org/goleak` to catch goroutines that outlive their test:

```go
func TestMain(m *testing.M) {
    goleak.VerifyTestMain(m)
}
```

A leaked goroutine in a node test usually means shutdown is broken -
that is a real bug, not test noise. Same for file handles: close DBs in
`t.Cleanup(func() { db.Close() })`.

## Fuzzing and property tests

Use native fuzzing (`go test -fuzz=FuzzX`) for anything that parses
bytes: wire protocol decoders, key encodings, manifest parsers.

```go
func FuzzDecodeFrame(f *testing.F) {
    f.Add([]byte{0, 0, 0, 3, 'a', 'b', 'c'})
    f.Fuzz(func(t *testing.T, data []byte) {
        _, _ = DecodeFrame(data) // must not panic, ever
    })
}
```

Property tests for storage: write a random sequence of Put/Delete
against both the real engine and a Go map, then compare full contents -
finds ordering and iterator bugs no example-based test will.

## Rules

- `-race` always; `-count=2 -shuffle=on` in CI to surface state leaks.
- A flaky test is a bug: fix it or delete it the day it flakes. A suite
  people retry is a suite people ignore.
- Two tiers: `go test ./...` fast and deterministic (seconds, every
  commit); heavy chaos/soak runs behind a build tag
  (`//go:build slow`) on a schedule.
- Never assert on log output or timing; assert on state and returned
  values.
