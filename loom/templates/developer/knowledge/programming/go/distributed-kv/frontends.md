# Administrative and user frontends

The HTTP surfaces of a databox-style system: a JSON client API and an
HTML admin UI. Both come from the standard library - `net/http` routing
(Go 1.22+ pattern syntax), `html/template`, `embed` - no framework, no
external assets. One binary serves everything it needs.

## Routing with the enhanced ServeMux (Go 1.22+)

Method + path patterns with wildcards are built in now; you do not need
a router dependency:

```go
mux := http.NewServeMux()

// client API (JSON)
mux.HandleFunc("GET /api/v1/kv/{key...}", s.handleGet)     // {key...} = rest-of-path
mux.HandleFunc("PUT /api/v1/kv/{key...}", s.handlePut)
mux.HandleFunc("DELETE /api/v1/kv/{key...}", s.handleDelete)
mux.HandleFunc("GET /api/v1/blobs/{id}", s.handleBlobGet)  // supports Range
mux.HandleFunc("POST /api/v1/blobs", s.handleBlobPut)

// admin UI (HTML) - separate mux, separate listener (see below)
adm.HandleFunc("GET /{$}", s.admOverview)                  // {$} = exactly "/"
adm.HandleFunc("GET /shards", s.admShards)
adm.HandleFunc("GET /nodes/{id}", s.admNode)
adm.HandleFunc("POST /nodes/{id}/drain", s.admDrain)

key := r.PathValue("key")                                   // in handlers
```

Precedence is most-specific-wins; `{$}` stops `/` from matching
everything. If the module must support pre-1.22 Go, take
`github.com/go-chi/chi/v5` (same shape, tiny); do not hand-roll prefix
matching.

## Middleware - the seam auth and logging share

```go
type mw func(http.Handler) http.Handler

func chain(h http.Handler, m ...mw) http.Handler {
    for i := len(m) - 1; i >= 0; i-- { h = m[i](h) }
    return h
}

srv := &http.Server{
    Addr:              cfg.APIAddr,
    Handler:           chain(mux, withRecover, withLog, withAuth(auth)),
    ReadHeaderTimeout: 5 * time.Second,     // slowloris guard - always set
}
```

`withAuth` resolves the bearer token to an `AuthCtx` (user-systems.md)
and stores it in the request context; handlers declare the permission
they need. The admin mux gets the same chain plus a PermAdmin check.
Run admin on its OWN listener/port so the network can fence it - not a
path prefix on the public API.

## JSON API conventions

- Encode errors as one shape everywhere:
  `{"error": {"code": "wrong_shard", "epoch": 7}}` - the routing
  errors from sharding.md must survive HTTP translation with their
  data intact.
- Write helpers once (`writeJSON(w, code, v)`, `readJSON(r, &req,
  maxBytes)` with `http.MaxBytesReader`) and use them in every
  handler; per-handler encoding drift is where API bugs breed.
- Blob upload is a streaming `POST` (chunked encoding), download
  supports `Range` (`http.ServeContent` if you can seek, manual ranges
  over chunk manifests otherwise - blob-storage.md read path).
- Version the path (`/api/v1/`); additive changes only within a
  version.

## Admin UI with html/template + embed

Server-rendered pages; zero JS build step; assets compiled into the
binary:

```go
//go:embed templates/*.html static/*
var uiFS embed.FS

var tpl = template.Must(template.ParseFS(uiFS, "templates/*.html"))

func (s *Server) admShards(w http.ResponseWriter, r *http.Request) {
    data := struct {
        Epoch  uint64
        Shards []ShardRow          // built from the metadata snapshot
        Ops    []OpRow             // running splits/moves
    }{...}
    if err := tpl.ExecuteTemplate(w, "shards.html", data); err != nil {
        log.Printf("render shards: %v", err)
    }
}
```

Template layout that stays maintainable: one `base.html` with
`{{block "content" .}}`, one file per page defining that block, tiny
view-model structs per page (never hand templates raw internal types -
the view model is the seam that keeps refactors from breaking pages).
`html/template` escapes by default; never use `template.HTML` on
anything derived from user input.

Forms for actions (drain node, trigger split, create user) are plain
`POST` + redirect; confirmation pages beat JS dialogs here. CSRF: since
the admin UI is cookie/token-authed, set `SameSite=Strict` on the
session cookie and require a hidden one-time token on mutating forms.

## What the admin UI must show (the operator's contract)

From day one (build-plan.md wires these to gates):

- **Cluster**: nodes with liveness state, capacity, leader counts.
- **Shards**: the table with ranges, groups, replicas, epoch, and any
  running op with its current step (metadata-group.md `op/<id>` -
  this is your window into a stuck split).
- **Storage**: per-node engine metrics (pebble.md `db.Metrics()`),
  scrub queue depth and last-pass age (blob-storage.md).
- **A raw metrics endpoint** (`/metrics`, Prometheus text format is
  trivial to emit by hand) so external monitoring needs nothing
  special.

## Serving and shutdown

```go
go func() { errCh <- srv.ListenAndServe() }()
<-stop                                        // signal.NotifyContext
ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
defer cancel()
srv.Shutdown(ctx)                             // drains in-flight requests
```

Both servers (API, admin) shut down before the node stops raft groups;
goleak in tests (../testing.md) will catch a forgotten one.
