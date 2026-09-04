# MVC monolith structure - kernel, apps, domain

How to structure a multi-app Go web monolith (e.g. a personal-cloud
suite: drive, mail, calendar, contacts behind one launcher) as three
layers in one binary: a KERNEL package holding the shared runtime
(router, sessions, auth wrappers, page chrome, audit), APP packages
holding controllers and co-located views, and DOMAIN packages holding
models and persistence. One process, one router, one design system -
but each app stays a package you can read, test, and delete in
isolation. This is MVC with Go package boundaries as the enforcement
mechanism: the compiler rejects a dependency in the wrong direction.

## The three layers and the dependency rule

```
cmd/<binary>/main.go      composition root: wires everything, fatals
        │                 on config/route errors, starts workers
        ▼
pkg/apps/<name>/          controllers + views (handlers, *.tpl, assets)
        │   imports kernel, domain, ui
        ▼
pkg/kernel/               shared runtime; imports domain, ui - NEVER apps
        ▼
pkg/domain/<name>/        models + persistence; imports ONLY the storage
                          client and pkg/domain/kvx helpers
pkg/ui/                   design system: base shell template, CSS/JS/font
                          assets, template FuncMap; imports nothing above
```

The rule, stated once and enforced by imports: apps → kernel → domain
→ storage. Domain packages never import the kernel, any app, or the ui
package - they know nothing about HTTP. The kernel never imports an
app; it defines the registration types (`Route`, `Mount`) apps satisfy.
The router is therefore closed over an interface-shaped contract, not a
package list, and deleting an app is deleting its directory plus one
line in main.

When a LOWER domain needs an UPPER one - users must create a personal
drive at signup, but drives imports users - do not invert the imports.
Give the lower package a function field and wire it in main:

```go
// in pkg/domain/users: a seam, not an import
type Store struct {
    DB       *client.Client
    OnSignup func(tx *client.Tx, u *User) // nil = no extra records
}

// in cmd/<binary>/main.go
userStore.OnSignup = func(tx *client.Tx, u *users.User) {
    id := kvx.NewID()
    drives.StagePersonalDrive(tx, id, u.Username)
    u.PersonalDrive = id
}
```

The signup and the drive still commit in ONE storage transaction; the
boundary survives. The same trick composes sibling domains (the mail
store's recipient typeahead is `mailStore.Contacts =
contactsStore.Match`) and lets background workers expose "kick" hooks
to apps without the app importing the worker.

## The kernel

The kernel is one struct bundling every domain store, the logger, and
platform config. Main constructs it once and hands the pointer to every
app's Mount:

```go
type App struct {
    Users    *users.Store
    Site     *site.Store
    Drives   *drives.Store    // …one field per domain store
    Log      *slog.Logger
    SecureCookies bool        // platform config rides along
    MaxUpload     int64
    SSE  *Hub                 // live-stream hub, wired by Router
    // rate limiters, parsed auth-page templates: unexported,
    // initialized in Router via sync.Once - no package-level state
}

// Ctx bounds one request's storage work.
func Ctx(r *http.Request) (context.Context, context.CancelFunc) {
    return context.WithTimeout(r.Context(), 15*time.Second)
}
```

Kernel files each own one concern: `router.go` (registration),
`auth.go` (Authed/AdminOnly wrappers), `sessions.go` (cookie + CSRF),
`chrome.go` (shell data), `respond.go` (form-vs-fetch replies),
`ratelimit.go`, `audit.go`, `sse.go`, plus the pre-auth pages
themselves (/login, /signup, /logout, /healthz, /static/).

## App registration

Every app package exposes exactly one entry point:

```go
func Mount(k *kernel.App) kernel.Mount
```

against two kernel types:

```go
type Route struct {
    Pattern string        // "GET /contacts" - net/http 1.22 patterns
    Handler http.Handler  // (gorilla-mux.md if you need richer routing)
}
type Mount struct {
    App    string         // owner name, for collision errors
    Routes []Route
}
```

Main passes every Mount to `k.Router(launcher.Mount(k),
drive.Mount(k), …)` and fatals on error. Router registers its own
kernel routes first, then every app's, and returns an error on ANY
duplicate pattern - naming the two apps that collided. Three
consequences worth copying:

- NO `init()` side effects, no blank imports, no global route table.
  Mount order is deterministic and visible in one place; a route
  collision is a startup crash, never a silent last-wins.
- Extra dependencies are Mount parameters: `mail.Mount(k, kickOutbound,
  kickMail func())` for worker nudges; an app with many seams takes a
  `Deps` struct of function values (`KickMail func()`, `Recheck
  func()`, a `Resolver` interface for DNS) so the app never imports the
  worker packages and tests inject fakes trivially.
- Feature flags gate at the route, not inside handlers:
  `k.Authed(k.FeatureGate("contacts", h.page))` 404s a disabled
  feature identically to an unbuilt route (it should not even confirm
  it exists); `k.FeatureGateHTTP(id, h)` is the plain-handler variant
  for asset and anonymous routes.

Internally each app builds a small `handlers` struct at Mount time -
the app's parsed template set alongside the kernel pointer - and hangs
its handler methods off it. No package-level state in apps either.

## Request lifecycle

`GET /contacts` walks: ServeMux pattern match → `Authed` wrapper
(reads the session cookie, loads the session record from the store -
any replica can serve any request because sessions live in the shared
store, not process memory - loads the user, destroys the session and
bounces to `/login?next=…` on any failure, rejects banned accounts) →
`FeatureGate` (404 when the feature is off) → the handler. Authed's
shape upgrades the handler signature so identity is an argument, never
a context lookup:

```go
func (a *App) Authed(
    h func(http.ResponseWriter, *http.Request, users.Session, users.User),
) http.HandlerFunc
```

`AdminOnly` wraps Authed and answers a plain 404 to non-admins.
Validate the post-login `next` target as local-absolute only (`/x`,
never `//host` or backslashes) so the redirect can't bounce off-site.

Every signed-in MUTATION handler starts with a CSRF check against the
session's token (constant-time compare; form field or `X-CSRF` header)
and finishes through one dual-mode responder:

```go
if !kernel.CheckCSRF(r, sess) {
    http.Error(w, "bad csrf token", http.StatusForbidden)
    return
}
// … do the work …
h.k.Respond(w, r, "/contacts?ok=saved", err, map[string]any{"node": id})
```

Respond inspects an `X-Requested-With: fetch` header the shared JS
sends: fetch callers get JSON `{ok, error?, …payload}` with a status
mapped from the error (`errors.Is` against known domain sentinel
errors → 404/401/403/409/507; unknown → 400), plain form posts get a
303 redirect with `?err=` on failure. One error-translation table in
the kernel keeps raw internals (paths, keys) out of the UI and gives
progressive enhancement for free: every page works without JS.

## Views: typed page structs and Chrome

Chrome is the shell data every page embeds - title, site name, theme,
current-app id for the switcher highlight, user, session, feature
switches, quota meter, unread badge, and the canonical app list that
both the switcher and the launcher grid iterate (ONE list, so they
can never drift). Pages are TYPED structs embedding it - never
`Data any`:

```go
type Page struct {
    kernel.Chrome
    Cards  []dcontacts.Entry   // domain structs render fine as-is…
    Drives []DriveVM           // …but view-only shapes get a VM struct
}

type DriveVM struct {           // view model: just what the select needs
    ID   string `json:"id"`
    Name string `json:"name"`
}

func (h *handlers) page(w http.ResponseWriter, r *http.Request,
    sess users.Session, user users.User) {
    pg := Page{Chrome: h.k.Chrome(r, "Contacts", "contacts", sess, user),
        Cards: cards, Drives: ds}
    ui.Render(w, h.views, "contacts", pg)
}
```

Reuse a domain struct directly when the template shows the record;
mint a small VM struct when the view needs a projection, a join, or a
JSON shape for the page's fetch API. Chrome construction soft-fails
every lookup (log a warning, render a zero badge) - a chrome widget is
never worth failing the page for.

Templates and assets are CO-LOCATED and embedded, one set per app,
parsed once at Mount over the shared base shell:

```go
//go:embed *.tpl
var tplFS embed.FS
//go:embed assets
var assetFS embed.FS

h := &handlers{k: k, views: ui.MustParse(tplFS)}
```

`ui.MustParse` parses the base shell plus the app's `*.tpl` with the
shared FuncMap and panics on error - a parse error is a programming
error and must die at startup, not per request (html-templates.md).
The app serves its own JS/CSS at `/<app>/assets/` from the embedded FS
with the same cache policy as the global `/static/`: long-lived
immutable for fonts, `no-cache` (revalidate) for CSS/JS so a UI fix
never reads as "didn't land".

## Domain packages: key schemas instead of an ORM

A domain package is a `Store` struct over the KV client plus the types
it owns. There is no ORM and no SQL - the schema IS the key design,
and it lives in code review, not migrations:

- ONE shared helper package (call it `kvx`) holds the storage idioms
  every domain uses - random key-safe IDs, JSON get/set, prefix
  scan/delete, OCC-conflict detection - and, critically, its `doc.go`
  is the CANONICAL KEY TABLE: every key family the whole platform
  writes, one line each (`/app/users/<username> → users.User`). Any
  new key is a review of that file. One namespace root (`/app/…`)
  means one storage grant covers the whole platform.
- Each domain declares its own prefixes as consts and never touches
  another domain's. List-by-owner needs a REVERSE INDEX row
  (`/app/userdrives/<user>/<driveID>`) written in the SAME transaction
  as the canonical row - an index that can drift from its source is a
  bug factory.
- Newest-first listings use inverted-timestamp IDs so a plain
  ascending prefix scan returns newest first, with no ORDER BY
  anywhere; retention becomes one delete-range past a cutoff prefix:

```go
// sorts newest first; random suffix breaks same-nanosecond ties
func InvIDAt(t time.Time) string {
    return fmt.Sprintf("%020d-%s",
        math.MaxInt64-t.UnixNano(), auth.RandomToken(3))
}
```

- Uniqueness (usernames, name claims) comes from OCC transactions:
  read the key (recording "did not exist"), write it, commit - racing
  writers conflict and one wins. Retry on the conflict error.
- IDs arrive in URLs - attacker-controlled - and become key segments.
  Validate EVERY user-supplied segment before it reaches a key: IDs
  must match the generator's alphabet (no separator character exists
  in it, so an id can never traverse out of its key position);
  user-chosen names get a charset/length rule of their own.
- Sentinel errors (`ErrNotFound`, `ErrAccessDenied`, …) are the
  domain's public error surface; the kernel's respond table maps them
  to statuses and user-safe messages. Handlers never build error
  strings.

## Auditing

Privileged mutations and their audit entries must commit TOGETHER.
Expose an append that writes into the caller's transaction, plus a
standalone form for actions with no surrounding transaction:

```go
kernel.AppendAudit(tx, kernel.AuditEntry{Actor: admin.Username,
    Action: "user.ban", Target: username})   // same commit or neither
a.Audit(r, user, sess, "impersonate.start", target, "")  // one-key txn
```

Entries land under one prefix keyed by inverted-timestamp id (newest
first, retention = one range delete), snapshot the actor's admin bit
at action time, and attribute an impersonating admin's actions to the
ADMIN with the impersonated member recorded alongside. Retention runs
as a periodic worker, never a per-request piggyback.

## Adding a new app end-to-end

1. Domain first: `pkg/domain/<name>/` - types, Store over the KV
   client, key prefixes. Add every new key family to the kvx key
   table doc. Unit-test against the fake store before any HTTP exists.
2. Register the feature id in the site feature registry so the flag,
   the launcher card, and the app switcher light up from one list.
3. `pkg/apps/<name>/` - `Mount(k *kernel.App) kernel.Mount`, a
   `handlers` struct, embedded `*.tpl` + `assets/`, typed page structs
   embedding `kernel.Chrome`. Wrap pages in
   `k.Authed(k.FeatureGate(id, …))`, mutations additionally behind
   `CheckCSRF` + `Respond`.
4. Add the store field to `kernel.App`, construct the store in main,
   and add `<name>.Mount(k)` to the Router call. Compile: a duplicate
   route now fails startup.
5. Tests: domain tests (fake store), a template render test, and a
   web test through the real router (below). Then wire any background
   worker in main with a kick-hook handed to Mount.

## Testing seams per layer

Each layer has a natural seam; use all three (../testing.md):

- **Domain**: a test-only in-memory fake of the storage server behind
  `httptest.NewTLSServer`, speaking exactly the client's wire shapes -
  so tests exercise the REAL store code (JSON plumbing, OCC
  transactions, blob paths) with no running cluster. `kvxtest.New(t)`
  returns a connected client; the server dies with the test. Compose
  real stores over it, including the main-wired hooks (`OnSignup`).
- **Views**: parse the app's real templates and execute them against a
  fixture page struct into a buffer; assert on stable markers (an
  element id, an escaped name, the asset URL) and on the empty state.
  This catches template/struct drift with zero HTTP.
- **Web** (`*_web_test.go`): a harness = fake store + a minimal
  `kernel.App` (logger to `io.Discard`) + `k.Router(Mount(k))`, driven
  by `httptest.NewRequest`/`NewRecorder` through the REAL router - so
  auth wrappers, feature gates, CSRF, and respond semantics are all in
  the loop. Give the harness `signIn(t, user)` returning a client that
  carries the session cookie and CSRF token, and `get`/`post` helpers;
  scenario tests then read as user stories (create → list → 303
  Location → view page markers → permission matrix: anonymous 401,
  authenticated-but-forbidden 404, feature off 404).

## Rules

- Dependency direction is law: apps → kernel → domain → storage. A
  domain package importing the kernel, an app, or the ui package is a
  layering bug - fix it with a function-field seam wired in main.
- One composition root. All construction, wiring, env parsing, and
  worker startup live in main; `init()` does nothing anywhere.
- Route registration is explicit and collision-checked at startup. If
  your router can't name the two owners of a duplicate pattern, add
  the owner map.
- Handlers receive identity (`sess, user`) as parameters from the auth
  wrapper - never from context lookups scattered through the stack.
- Every signed-in mutation: CSRF check first, dual-mode Respond last.
  Every mutation route is a POST; GETs never mutate.
- Typed page structs embedding Chrome, always. `Data any` templates
  rot silently; typed pages fail in the render test.
- New storage keys go through the canonical key table review. Reverse
  indexes commit in the same transaction as their canonical row.
- Validate every URL-supplied segment before it becomes a key
  segment; the ID alphabet must exclude the key separator.
- Chrome/badge/status lookups soft-fail with a log line; page data
  lookups fail the page. Know which one you're writing.
- Keep the process stateless: sessions, CSRF tokens, and rate-limit
  state that must survive a restart live in the store; in-memory
  limiters are per-replica defense in depth only.
