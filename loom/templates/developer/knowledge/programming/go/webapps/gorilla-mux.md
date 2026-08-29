# HTTP routing with gorilla/mux — routers, middleware, real servers

How to build the HTTP layer of a Go web application: a gorilla/mux
router with method matching and path variables, subrouters carrying
scoped middleware, apache-style access logging, and the `http.Server`
wiring around it — timeouts, TLS, graceful shutdown. The router is the
skeleton the templates (html-templates.md) and handlers (mvc.md) hang
off; get its order and its middleware layering right and everything
else stays boring. The last section says when the stdlib `ServeMux`
is enough and you should skip the dependency entirely.

```
go get github.com/gorilla/mux
```

## Router setup

`mux.NewRouter()` matches routes in REGISTRATION ORDER — first match
wins. That single fact dictates the layout: exact and specific routes
first, prefix routes after, the catch-all dead last.

```go
r := mux.NewRouter()

// Exact path + method. Unlisted methods on a matched path answer 405.
r.HandleFunc("/login", loginPage).Methods(http.MethodGet)
r.HandleFunc("/login", loginSubmit).Methods(http.MethodPost)
r.HandleFunc("/", dashboard).Methods(http.MethodGet) // exact "/" only

// Path variables, read with mux.Vars(r).
r.HandleFunc("/users/{name}", userDetail).Methods(http.MethodGet)

// Constrained variables: regex after a colon. `[0-9]+` rejects junk
// at the router (404) so the handler never parses garbage, and an
// alternation makes unknown values 404 instead of reaching code:
r.HandleFunc("/admin/shards/{gid:[0-9]+}/split", shardSplit).Methods(http.MethodPost)
r.HandleFunc("/admin/{target:rebalance|split|repair}/{action:pause|resume}",
    adminPause).Methods(http.MethodPost)

// `{key:.*}` lets the variable CONTAIN SLASHES — the way to route
// hierarchical keys ("/api/v1/kv/a/b/c" → key = "a/b/c"):
r.HandleFunc("/kv/{key:.*}", kvGet).Methods(http.MethodGet)
r.HandleFunc("/kv/{key:.*}", kvSet).Methods(http.MethodPut)
r.HandleFunc("/kv/{key:.*}", kvDelete).Methods(http.MethodDelete)

key := mux.Vars(r)["key"]          // inside the handler
```

Unmatched requests go to `r.NotFoundHandler` — set it to something
structured (JSON for `Accept: json` or `/api/` paths, HTML for
browsers) instead of the bare stdlib 404. `MethodNotAllowedHandler`
is its 405 sibling.

## Subrouters and PathPrefix

`PathPrefix(...).Subrouter()` groups a subtree so a path prefix and a
middleware stack are stated once:

```go
v1 := r.PathPrefix("/api/v1").Subrouter()
v1.HandleFunc("/auth/login", login).Methods(http.MethodPost)
v1.HandleFunc("/list", list).Methods(http.MethodGet)

in := r.PathPrefix("/internal").Subrouter()
in.Use(pskAuthMiddleware)          // applies to /internal/* ONLY
in.HandleFunc("/propose", propose).Methods(http.MethodPost)
```

`r.Use(m)` on the root router wraps every matched route;
`sub.Use(m)` only that subtree. Middleware added with `Use` runs
around ROUTE MATCHES — the NotFoundHandler is not wrapped, which is
one reason access logging (below) is often mounted around the whole
router instead.

Grow past one file by giving each package a mount function instead of
`init()` side effects — deterministic order, no import cycles (the
route packages import the app core, the main package passes mounts
in):

```go
type RouteMounter func(r *mux.Router, s *Server)
for _, mount := range mounters { mount(r, s) }
```

A GUI package that owns the catch-all mounts LAST:

```go
r.PathPrefix("/").HandlerFunc(gui.notFound) // claims everything left
```

## Static assets

Serve embedded assets from a prefix route; strip the prefix and clean
the path yourself so traversal tricks die at the door:

```go
r.PathPrefix("/assets/").HandlerFunc(asset).Methods(http.MethodGet)

func asset(w http.ResponseWriter, r *http.Request) {
    name := strings.TrimPrefix(r.URL.Path, "/assets/")
    name = path.Clean(name) // "..%2f" games normalize away
    if name == "." || strings.HasPrefix(name, "..") ||
        strings.HasSuffix(name, ".go") { // embed `*` embeds source too
        http.NotFound(w, r)
        return
    }
    data, err := assetsFS.ReadFile(name)
    if err != nil { http.NotFound(w, r); return }
    ct := mime.TypeByExtension(path.Ext(name))
    if ct == "" { ct = "application/octet-stream" }
    w.Header().Set("Content-Type", ct)
    w.Header().Set("Cache-Control", "max-age=300") // binary-versioned
    _, _ = w.Write(data)
}
```

## Middleware and the status-recording ResponseWriter

A mux middleware is `func(http.Handler) http.Handler`; `Use` accepts
it directly (or wrapped as `mux.MiddlewareFunc`). Anything that wants
the response status or size must wrap the writer, because handlers
write THROUGH the interface and never report back:

```go
type logRecorder struct {
    http.ResponseWriter
    status int
    bytes  int64
}

func (r *logRecorder) WriteHeader(code int) {
    r.status = code
    r.ResponseWriter.WriteHeader(code)
}

func (r *logRecorder) Write(p []byte) (int, error) {
    n, err := r.ResponseWriter.Write(p)
    r.bytes += int64(n)
    return n, err
}

// Forward Flush or every streaming handler (SSE, NDJSON) silently
// stops streaming behind this wrapper.
func (r *logRecorder) Flush() {
    if f, ok := r.ResponseWriter.(http.Flusher); ok { f.Flush() }
}

// Unwrap lets http.NewResponseController reach the real writer
// (deadlines, hijack) through the wrapper.
func (r *logRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }
```

Initialize `status: http.StatusOK`. This is load-bearing: a handler
that calls `Write` without `WriteHeader` triggers the implicit 200 on
the UNDERLYING writer — your wrapper's `WriteHeader` is never called
and an uninitialized field would log 0.

## Apache-style access logging

The Common Log Format is `%h %l %u %t "%r" %>s %b` — remote host,
identd (always `-`), authenticated user, timestamp, request line,
final status, body bytes. The Combined format appends
`"%{Referer}i" "%{User-agent}i"`. Every log shipper ever written
parses these. The Go time layout for `%t` is
`02/Jan/2006:15:04:05 -0700`.

```go
func accessLog(dst io.Writer) mux.MiddlewareFunc {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            rec := &logRecorder{ResponseWriter: w, status: http.StatusOK}
            next.ServeHTTP(rec, r)
            host, _, err := net.SplitHostPort(r.RemoteAddr)
            if err != nil { host = r.RemoteAddr }
            // Combined Log Format:
            // host - user [ts] "METHOD uri PROTO" status bytes "ref" "ua"
            fmt.Fprintf(dst, "%s - %s [%s] %q %d %d %q %q\n",
                host, dash(userFrom(r)),
                time.Now().Format("02/Jan/2006:15:04:05 -0700"),
                r.Method+" "+r.RequestURI+" "+r.Proto,
                rec.status, rec.bytes,
                dash(r.Referer()), dash(r.UserAgent()))
        })
    }
}

func dash(s string) string { if s == "" { return "-" }; return s }
```

Details that matter: `%q` on the request line, referer, and UA is
deliberate — request paths and headers are ATTACKER-CONTROLLED, and
`%q` escapes quotes and control bytes so no request can inject fake
log lines. Log after `next.ServeHTTP` returns so `%>s` is the final
status (record `time.Now()` before the call if you also want a
duration field). Serialize `dst` writes (a `log.Logger` or a mutexed
writer) — concurrent `Fprintf` to one file interleaves. `r.RemoteAddr`
is the TCP peer; substitute the last `X-Forwarded-For` hop ONLY when
a trusted proxy sits in front. Mount the middleware OUTERMOST so
rejections from auth and rate limiting are logged too. Prefer wrapping
the whole router (`srv.Handler = accessLogHandler(r)`) over `r.Use`
if 404s must appear in the log.

## Auth layering and rate limiting

Order, outermost first: access log → rate limit → authentication →
authorization → handler. Logging sees everything; limiting spends no
crypto on floods; authn establishes identity; authz checks it against
the resource.

Prefix-wide gates fit subrouter middleware (`in.Use(pskAuth)`).
Per-route auth reads better as typed handler wrappers — no context
smuggling, the signature proves the handler cannot run unauthed:

```go
func (a *App) Authed(h func(http.ResponseWriter, *http.Request, Session, User)) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        sess, user, ok := a.resolveSession(w, r) // redirect to /login on !ok
        if !ok { return }
        h(w, r, sess, user)
    }
}

func (a *App) AdminOnly(h func(http.ResponseWriter, *http.Request, Session, User)) http.HandlerFunc {
    return a.Authed(func(w http.ResponseWriter, r *http.Request, s Session, u User) {
        if !u.IsAdmin { http.NotFound(w, r); return } // don't confirm it exists
        h(w, r, s, u)
    })
}
```

Rate limit login and signup by BOTH client IP and target username,
BEFORE verifying credentials (that is the expensive, probeable step).
A per-key token bucket in a mutexed map is plenty; cap the map size
and sweep full buckets so key-rotating attackers can't grow memory. A
nil limiter must allow — rate limiting may only ever fail open. Answer
429 with `Retry-After`; bearer-API 401s carry
`WWW-Authenticate: Bearer realm="..."` per RFC 6750, and give one
message for every failure mode (unknown user, bad secret, expired) so
probers learn nothing.

## SSE and streaming endpoints

Streams break every buffering assumption, so they get an explicit
handshake: the event-stream headers, the server's per-connection
deadlines cleared (the stream is long-lived BY DESIGN while everything
else keeps its timeouts), and an opening comment flushed so the
client's `onopen` fires:

```go
func StartSSE(w http.ResponseWriter) (*http.ResponseController, error) {
    w.Header().Set("Content-Type", "text/event-stream")
    w.Header().Set("Cache-Control", "no-cache")
    w.Header().Set("X-Accel-Buffering", "no") // nginx: don't buffer
    rc := http.NewResponseController(w)
    _ = rc.SetReadDeadline(time.Time{})
    _ = rc.SetWriteDeadline(time.Time{})
    if _, err := fmt.Fprint(w, ": connected\n\n"); err != nil {
        return nil, err
    }
    return rc, rc.Flush()
}
```

Flush after every event; exit the write loop on `r.Context().Done()`
(fires on client disconnect). The same shape serves NDJSON watch
streams: set the Content-Type, `WriteHeader(200)`, then encode + Flush
per event. Do any preflight validation BEFORE the first write — once
the 200 is out, errors can only be an in-band trailer line, so a
resume-from-compacted-revision must answer its 410 up front. Count
concurrent streams per user (a mutexed `map[string]int` with a cap of
~12) or one browser pins unbounded goroutines. This is also why
middleware wrappers MUST forward `Flush` and expose `Unwrap` — an
access logger that swallows either silently kills every stream behind
it.

## Server wiring: timeouts, TLS, graceful shutdown

```go
srv := &http.Server{
    Addr:    listen,
    Handler: r,
    // Bounds the header-read phase — the classic slowloris vector.
    ReadHeaderTimeout: 10 * time.Second,
    // Reaps idle keep-alive conns so parked sockets free goroutines.
    IdleTimeout: 60 * time.Second,
    // Read/WriteTimeout: see below.
}

go func() {
    stop := make(chan os.Signal, 1)
    signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
    <-stop
    ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
    defer cancel()
    _ = srv.Shutdown(ctx) // stop accepting, drain in-flight, then close
}()
if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
    log.Error("server", "err", err)
    os.Exit(1)
}
```

`Shutdown` makes `ListenAndServe` return `http.ErrServerClosed` — that
is the CLEAN exit, never an error. SIGTERM first is what Kubernetes
sends. In a larger process, shut down in dependency order: cancel the
work context, join background goroutines (bounded wait), THEN
`srv.Shutdown`, then close stores — closing a store under a goroutine
still using it is a panic on exit.

Whole-request `ReadTimeout`/`WriteTimeout` sever anything longer than
the limit. If the server streams large uploads/downloads or holds
watch/SSE connections, either leave them UNSET and cap request SIZE
per handler with `http.MaxBytesReader(w, r.Body, cap)` (its overflow
surfaces as `*http.MaxBytesError` via `errors.As` → map to 413), or
set them generously (an hour) and have SSE clear its own deadlines via
`http.ResponseController` as above. `ReadHeaderTimeout` stays set
regardless — it costs streams nothing.

TLS on the same server:

```go
srv.TLSConfig = &tls.Config{
    MinVersion: tls.VersionTLS13,
    // A closure, not a static cert: rotation takes effect without
    // restarting the listener.
    GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
        return currentCert(), nil
    },
    // Request (never Require) when one port serves browsers AND
    // mTLS peers: peers present a cert, everyone else sends nothing.
    ClientAuth: tls.RequestClientCert,
}
go func() { errC <- srv.ListenAndServeTLS("", "") }() // certs from TLSConfig
select { // fail fast on bind errors instead of "running" while dead
case err := <-errC:
    return fmt.Errorf("https listener: %w", err)
case <-time.After(300 * time.Millisecond):
    return nil
}
```

With `RequestClientCert` the TLS layer enforces nothing — verify the
presented chain per-route in middleware: `r.TLS.PeerCertificates[0]`
is the leaf, `Verify` it against your CA pool with
`x509.ExtKeyUsageClientAuth`, and 401 on failure. That converts mTLS
from a per-connection property into a per-route policy on a shared
port.

## When plain net/http suffices

Since Go 1.22 `http.ServeMux` patterns carry methods and wildcards:
`"GET /login"`, `"POST /login"`, `"GET /users/{name}"`
(`r.PathValue("name")`), `"GET /static/"` (trailing slash = subtree).
For an app whose routes are exact paths plus a few subtrees, that is
the whole requirement — zero dependencies, and precedence is by
SPECIFICITY, not registration order. Keep registration explicit
anyway: collect `{Pattern, Handler}` mounts per feature package, track
`pattern → owner` in a map, and error at startup on duplicates naming
both owners — stdlib conflicts panic with less helpful messages, and
`init()`-based registration makes mount order nondeterministic.

gorilla/mux earns its import when you need: regex-constrained
variables (`{gid:[0-9]+}`, alternations), slash-containing variables
(`{key:.*}` — stdlib `{key...}` covers only trailing rest-of-path),
subrouters with SCOPED middleware, or `mux.CurrentRoute(r)` +
`route.GetPathTemplate()` — the low-cardinality route name ("/users/
{name}", not "/users/alice") that metrics and trace spans need to
avoid label explosions. Everything in this file except those four
features works identically on either router; the middleware,
recorder, SSE, and server sections are pure `net/http`.

## Rules

- Registration order is match order: specific before prefix,
  `PathPrefix("/")` catch-alls dead last, after every other mount.
- Never log or meter the raw `r.URL.Path` as a label — use the route
  template (`GetPathTemplate`) or cardinality explodes.
- Every ResponseWriter wrapper forwards `Flush` and implements
  `Unwrap`, or streaming and `http.ResponseController` break silently
  behind it. Initialize captured status to 200 — implicit
  WriteHeader bypasses wrappers.
- Access log outermost; write one line per request AFTER the handler
  returns; `%q` untrusted fields (path, referer, UA) against log
  injection; serialize writes to the destination.
- Rate limit before credential verification; limiter absence fails
  open; identical error messages for all auth failure modes.
- No whole-request Read/WriteTimeout on servers that stream — bound
  headers (`ReadHeaderTimeout`), idle conns (`IdleTimeout`), and body
  SIZE (`MaxBytesReader`) instead; SSE clears its own deadlines.
- `http.ErrServerClosed` after `Shutdown` is success, not an error;
  drain with a deadline and stop dependencies in order.
- Static/asset handlers `path.Clean` and reject `..` and source
  files (`embed` with `*` embeds your `.go` files too).
- Constrain path variables in the route (`[0-9]+`, alternations) so
  invalid input 404s before handler code runs.
- Test routing with `net/http/httptest`: `httptest.NewServer(r)` and
  real requests catch order and method bugs tables can't
  (../testing.md).
