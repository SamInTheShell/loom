# urfave/cli v3 — multi-command CLIs in Go

How to build a real multi-command CLI — one binary that is the
server, the operator tooling, and the client utilities at once (e.g. a
distributed KV store: `x server`, `x cluster status`, `x user create`,
`x console`) — on `github.com/urfave/cli/v3`. The library gives you a
nestable command tree, typed flags with env-var binding, generated
help, and context propagation. Use it when the tree has real depth;
for a satellite binary with two subcommands (`setup`, `run`), a
`switch os.Args[1]` plus stdlib `flag.NewFlagSet` is less code and
zero dependencies — don't drag in a framework for that.

```
go get github.com/urfave/cli/v3
```

## v3 is NOT v2 — unlearn these first

Most published examples are v2. v3 changed the core API; v2 habits
produce code that does not compile. The differences:

- There is no `cli.App` and no `cli.Context`. The root of the tree is
  a `*cli.Command`, and subcommands are `*cli.Command` too — one type,
  nested arbitrarily deep via `Commands: []*cli.Command`.
- `Run` takes a context first: `root.Run(ctx, os.Args)` — not
  `app.Run(os.Args)`.
- Handler signatures take `(context.Context, *cli.Command)`:
  - `Action func(ctx context.Context, cmd *cli.Command) error`
    (the named type is `cli.ActionFunc`)
  - `Before func(ctx context.Context, cmd *cli.Command)
    (context.Context, error)` — Before RETURNS a context; return the
    one you were given, or a derivative to replace it downstream.
  - `After func(ctx context.Context, cmd *cli.Command) error`
- Flag values are read off the command: `cmd.String("listen")`,
  `cmd.Bool("force")`, `cmd.Int("replicas")` (returns `int`),
  `cmd.Duration("ttl")`, `cmd.StringSlice("scope")`,
  `cmd.IsSet("listen")`, `cmd.Args()` — not off a `cli.Context`.
- Env binding moved from `EnvVars: []string{...}` to a value-source
  chain: `Sources: cli.EnvVars("X_PASSWORD")`. `cli.Files(paths...)`
  exists for file-backed sources; multiple keys in one `EnvVars` are
  tried in order, first present wins.
- Flag types are generic aliases (`cli.StringFlag`, `cli.BoolFlag`,
  `cli.IntFlag`, `cli.DurationFlag`, `cli.StringSliceFlag`, plus
  sized variants `Int64Flag`, `FloatFlag`, …). Fields you will use:
  `Name`, `Value` (the default), `Usage`, `Aliases`, `Sources`,
  `Required`, `Hidden`.
- Parent flags are visible to subcommands by default (lookup walks the
  command lineage); set `Local: true` on a flag to confine it.

## The command tree

`main.go` stays thin: the root command, signal wiring, and error
reporting. Each command family is a constructor function returning
`*cli.Command`, living in its own file of `package main`
(`server_cmd.go`, `ops_cmd.go`, `console_cmd.go`, …):

```go
func main() {
    root := &cli.Command{
        Name:  "x",
        Usage: "distributed key-value store in a single binary",
        Commands: []*cli.Command{
            serverCommand(),
            clusterCommand(),
            userCommand(),
            versionCommand(),
        },
    }
    ctx, stop := signal.NotifyContext(context.Background(),
        syscall.SIGINT, syscall.SIGTERM)
    defer stop()
    if err := root.Run(ctx, os.Args); err != nil {
        fmt.Fprintln(os.Stderr, "error:", err)
        os.Exit(1)
    }
}
```

`signal.NotifyContext` is the whole signal story: the context Run
receives is the context every Action receives; SIGINT/SIGTERM cancels
it, a blocking server returns, deferred cleanup runs, and the process
exits through the normal error path. No signal channel plumbing per
command.

Nesting is just more `Commands`; grouping commands (`cluster`,
`admin`) carry no Action of their own — invoking them prints help:

```go
func clusterCommand() *cli.Command {
    return &cli.Command{
        Name:  "cluster",
        Usage: "cluster lifecycle: status, join tokens, decommission",
        Commands: []*cli.Command{
            {
                Name:    "status",
                Aliases: []string{"info"},
                Usage:   "topology, shard health, active alerts",
                Flags:   connFlags(),
                Action:  clusterStatusAction,
            },
            {
                Name:      "decommission",
                Usage:     "drain and remove a node",
                ArgsUsage: "<node-id>",
                Flags: append(connFlags(),
                    &cli.BoolFlag{Name: "force",
                        Usage: "remove a dead node without draining"}),
                Action: decommissionAction,
            },
        },
    }
}
```

`Aliases` works on commands and flags both. Near-identical siblings
(pause/resume families, put/delete pairs) come from factory closures —
a function returning `*cli.Command` or a `cli.ActionFunc` with the
varying strings closed over — not copy-paste.

## Flags: declare shared sets as functions

Flag structs are stateful pointers: parsing mutates them (value,
set-count, applied marker). Never share one flag instance between two
commands — define shared sets as FUNCTIONS returning a fresh
`[]cli.Flag` and call them per command. Extend per command with
`append`:

```go
// connFlags: every command that talks to the cluster takes these.
func connFlags() []cli.Flag {
    return []cli.Flag{
        &cli.StringFlag{Name: "endpoint", Value: "localhost:8443",
            Usage: "cluster endpoint (host:port)"},
        &cli.StringFlag{Name: "user", Value: "root"},
        &cli.StringFlag{Name: "password",
            Usage:   "password (prompted if required and not given)",
            Sources: cli.EnvVars("X_PASSWORD")},
        &cli.StringFlag{Name: "output", Aliases: []string{"o"},
            Value: "text", Usage: "output format: text|json|yaml"},
    }
}

Flags: append(connFlags(),
    &cli.StringFlag{Name: "ttl", Value: "1h"}),
```

To reuse a set with one different default, clone the element — the
originals are shared by every other caller of the function:

```go
flags := connFlags()
for i, f := range flags {
    if sf, ok := f.(*cli.StringFlag); ok && sf.Name == "endpoint" {
        clone := *sf
        clone.Value = "x-service:8443" // in-cluster default
        flags[i] = &clone
    }
}
```

Precedence per flag is: command-line value > `Sources` (env/file,
first key found) > `Value` default. `cmd.IsSet(name)` is true when the
value came from the command line OR a source — only the untouched
default reports false. That property drives layered configuration
(defaults ← config file ← env ← flags) for a server: load defaults,
overlay the file named by `--config`, overlay environment, then apply
ONLY the flags where `cmd.IsSet` is true, so an unset flag's default
never stomps a file-provided value:

```go
if cmd.IsSet("listen") {
    cfg.Listen = cmd.String("listen")
}
```

Bind secrets (`--password`, `--secret-key`) and automation knobs to
env vars via `Sources` so scripts never put credentials in `argv`
(visible in `ps`); accept the standard names where they exist
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).

## Positional arguments

`cmd.Args()` returns an interface with `First()`, `Get(i)`, `Len()`,
`Tail()`, `Present()`, `Slice()`. There is no declarative arity —
validate manually and make the error the usage line:

```go
if cmd.Args().Len() != 1 {
    return fmt.Errorf("usage: x cluster decommission <node-id> [--force]")
}
```

Set `ArgsUsage: "<node-id>"` so generated help shows the shape. A
positional that starts with `-` (e.g. `-` for stdin) is eaten by flag
parsing; users must write `--` first — say so in the Usage string of
any command that accepts one.

## Before/After hooks

`Before` on the root runs once before any action — the place for
process-wide setup that depends on WHICH subcommand is running. Inside
the root's Before, `cmd` is the root and `cmd.Args().First()` is the
subcommand name:

```go
Before: func(ctx context.Context,
    cmd *cli.Command) (context.Context, error) {
    if cmd.Args().First() == "server" {
        shutdown = telemetry.Init("x", logger()) // long-running only
    }
    return ctx, nil // ALWAYS return the ctx
},
After: func(_ context.Context, _ *cli.Command) error {
    if shutdown == nil {
        return nil
    }
    // Fresh context: the run context is already canceled on
    // SIGINT/SIGTERM, and the final flush needs a live one.
    ctx, cancel := context.WithTimeout(
        context.Background(), 5*time.Second)
    defer cancel()
    return shutdown(ctx)
},
```

`After` runs even when the action returned an error or the context was
canceled — which is exactly why cleanup there must build its own
bounded context instead of reusing the (dead) run context.

## Long-running vs one-shot commands

A server command's Action builds configuration, constructs the server,
and returns its blocking run — cancellation via the signal context IS
the shutdown path:

```go
Action: func(ctx context.Context, cmd *cli.Command) error {
    cfg, err := buildConfig(cmd)
    if err != nil {
        return err
    }
    s, err := server.New(cfg, logger())
    if err != nil {
        return err
    }
    return s.Run(ctx) // blocks until ctx canceled or fatal error
},
```

One-shot client commands share a `dial(ctx, cmd)` helper that turns
the connection flag set into an authenticated client (endpoint, cert
pinning, token or interactive password prompt via
`golang.org/x/term.ReadPassword` on stderr), then do one request and
render the result. Centralize rendering in an `emit(cmd, v)` helper
switching on `cmd.String("output")` (`text|json|yaml`) so every
command is scriptable for free; prompts and warnings go to stderr,
data to stdout.

## File layout

- `main.go` — root command, signal context, logger, shared config
  assembly, trivial commands (`version`, `config show`).
- one file per command family (`server_cmd.go`, `ops_cmd.go`,
  `utils_cmd.go`, …), each exposing `xxxCommand() *cli.Command`; the
  helpers an action needs live next to it.
- shared flag-set functions live beside their consumer group
  (`connFlags` next to `dial`).
- keep actions thin: parse/validate here, then call into a library
  package (`pkg/server`, `pkg/client`) that never imports the CLI —
  that package is what tests exercise (testing.md); the command layer
  is glue you verify by running the binary.

## Rules

- `root.Run(ctx, os.Args)` — context first. `app.Run(os.Args)` is v2
  and does not compile against v3.
- `Before` returns the context every later hook and action receives —
  it is where you attach values or tracing spans. Return the incoming
  ctx (or your derivative); a nil return is ignored, which means a
  derived context you forgot to return is silently dropped.
- Never reuse a flag pointer across commands; flag structs accumulate
  parse state. Fresh `[]cli.Flag` from a function per command; clone
  before changing a default.
- Match accessor to flag type: `cmd.Int` for `IntFlag`, `cmd.Int64`
  for `Int64Flag`, `cmd.Duration` for `DurationFlag`. A wrong-type or
  misspelled name returns the zero value (and trips the invalid-flag
  handler) — it is not a compile error, so typos hide in accessors.
- Flag > env source > default is the value precedence; `IsSet` is true
  for flag AND env — use it to layer config without letting defaults
  overwrite file-loaded values.
- Cleanup in `After` needs `context.Background()` + timeout; the run
  context is already canceled when a signal ended the process.
- Return errors from Actions; let main print once and `os.Exit(1)`.
  `cli.Exit(msg, code)` exists when a specific exit code matters.
- Validate positional arity yourself and put the full usage line in
  the error; set `ArgsUsage` for help output.
- Grouping commands get no Action — bare invocation printing help is
  correct behavior, not a bug to paper over.
- Two subcommands and no growth ahead: stdlib `flag` + a switch beats
  the dependency.
