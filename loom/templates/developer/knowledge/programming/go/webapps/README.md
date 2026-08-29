# Go web applications — the assembly map

This folder covers building server-rendered web applications in Go
from the standard library up: routing and serving, HTML templating,
and the MVC structure that keeps a multi-app monolith maintainable.
The three files are layers of one stack — read them together when
building an app, or individually when retrofitting one layer.

- **gorilla-mux.md** — the HTTP layer: gorilla/mux routers, path
  variables, subrouters, middleware chains, apache-style (Combined
  Log Format) access logging with a status-capturing ResponseWriter,
  rate limiting, SSE, TLS, timeouts, graceful shutdown — and when
  plain `net/http` ServeMux is enough.
- **html-templates.md** — the view layer: `html/template` files
  embedded with `go:embed`, base-layout composition schemes, a
  buffer-first renderer with real status codes, FuncMap conventions,
  typed view models, and the test pattern that renders every
  template.
- **mvc.md** — the structure: kernel (shared runtime: router,
  sessions, auth, chrome), apps (controllers + co-located templates),
  domain (models over a KV store), the dependency rule the compiler
  enforces, and the end-to-end recipe for adding a new app.

A worked example of this structure hosting a heavyweight domain is
`../git-hosting/` (a git forge as one app + domain pair). The wider
service patterns these apps sit behind are `../gateway-relay.md`
(public ingress) and `../protocols.md` (every wire in the stack).
Testing seams for all three layers: `../testing.md`.
