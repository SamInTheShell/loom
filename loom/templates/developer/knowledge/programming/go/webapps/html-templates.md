# HTML templating - html/template with embedded layouts

Server-rendered HTML in Go: `html/template` files compiled into the
binary with `go:embed`, composed as base-layout + page, executed
through a small renderer package. The payoff is a web UI that ships as
one binary with zero files on disk (works air-gapped, trivial to
deploy) and whose templates are parsed ONCE at startup - a syntax
error fails the boot and the unit test, never a user's request. Pages
render from typed view-model structs; escaping is automatic and
context-aware. Routing that calls these renders is in
[gorilla-mux.md](gorilla-mux.md); where handlers end and views begin
is in [mvc.md](mvc.md); test discipline is in
[../testing.md](../testing.md).

## Embedding the template files

Templates live as `.tpl` files next to Go code and get embedded:

```go
// Package templates holds the portal's HTML templates. Embedded so
// the GUI works from a single binary with zero files on disk.
package templates

import "embed"

//go:embed *.tpl
var FS embed.FS
```

Two placements, both correct:

- One shared `pkg/templates` package for a single-app portal - every
  page in one directory, one renderer parses them all.
- Per-app co-location for a multi-app platform: each app package
  embeds its OWN `.tpl` files beside its handlers
  (`//go:embed *.tpl` in `pkg/apps/contacts/contacts.go` next to
  `contacts.tpl`), and parses them on top of a shared base shell at
  mount time. Deleting the app directory deletes its views; nothing
  orphans.

The embed pattern `*.tpl` is deliberate - it cannot pick up `.go`
source. If you ever embed with `*` (mixed asset dirs), the Go source
is embedded too and MUST be blocked at the serving layer (below).

## Base layout + content block - per-page template sets

The shell (doctype, head, nav, error banner) lives in `base.tpl` and
delegates to a block each page defines:

```html
<!doctype html>
<html lang="en">
<head><title>{{if .Title}}{{.Title}} - {{end}}myapp</title>
<link rel="stylesheet" href="/assets/style.css"></head>
<body>
{{if .User}}<nav>
  <a href="/" {{if eq .Path "/"}}class="active"{{end}}>Dashboard</a>
  {{if .Admin}}<a href="/users">Users</a>{{end}}
</nav>{{end}}
<main>
{{if .Error}}<div class="banner banner-error">{{.Error}}</div>{{end}}
{{template "content" .}}
</main>
</body>
</html>
```

Every page file defines only its block:

```html
{{define "content"}}
{{with .Data}}
<h1>{{.Status}} - {{if eq .Status 404}}Not found{{else}}Error{{end}}</h1>
{{if .Detail}}<p class="muted">{{.Detail}}</p>{{end}}
{{end}}
<p><a href="/">← back</a></p>
{{end}}
```

The composition mechanism matters: a Go template SET has one flat
namespace, so two files both defining `"content"` collide. Therefore
build ONE SET PER PAGE - each set contains exactly base + that page:

```go
func New() (*Renderer, error) {
    names, err := fs.Glob(templates.FS, "*.tpl")
    if err != nil { return nil, err }
    r := &Renderer{pages: map[string]*template.Template{}}
    for _, name := range names {
        if name == "base.tpl" { continue } // layout is not a page
        t, err := template.New(name).Funcs(funcs).
            ParseFS(templates.FS, "base.tpl", name)
        if err != nil { return nil, fmt.Errorf("parse %s: %w", name, err) }
        r.pages[name] = t
    }
    if len(r.pages) == 0 { return nil, fmt.Errorf("no page templates") }
    return r, nil
}
```

Render executes the LAYOUT, which pulls in the page's block:
`t.ExecuteTemplate(&buf, "base.tpl", page)`. Register the FuncMap with
`.Funcs(funcs)` BEFORE `ParseFS` or parsing fails on unknown
functions. Provide `MustNew()` that panics, for wiring paths where a
broken template should stop the process.

### Alternative: named pages bracketing top/bottom

When many packages share one shell plus a library of partials (icon
glyphs, avatar chips), invert the scheme: the base defines `"top"`
and `"bottom"` halves plus named partials, and each page is a
uniquely NAMED define that brackets itself:

```html
{{define "launcher"}}{{template "top" .}}
<h1>{{.Greeting}}, {{.User.DisplayName}}</h1>
{{template "bottom" .}}{{end}}
```

```go
// MustParse: the base shell plus one app's *.tpl files, one set per
// app, built once at mount time (nil app = base only).
func MustParse(app fs.FS) *template.Template {
    t := template.New("").Funcs(Funcs)
    template.Must(t.ParseFS(baseFS, "*.tpl"))
    if app != nil { template.Must(t.ParseFS(app, "*.tpl")) }
    return t
}
```

Here page names (not block names) must be unique within an app, and
execution targets the page: `t.ExecuteTemplate(w, "launcher", pg)`.
Pick per-page sets when every page has the same shell shape; pick
top/bottom when apps need several pages per set and shared partials.

## Typed view models

Templates receive one struct. Two conventions, in order of maturity:

1. A shared envelope with an `any` payload - quick to start:

```go
type Page struct {
    Title string // "<Title> - myapp" in the tab
    User  string // empty on login page → nav hidden
    Admin bool   // nav gating only; server enforces access regardless
    CSRF  string // per-session anti-forgery token for mutating forms
    Path  string // current path, highlights the active nav link
    Error string // red banner when set
    Data  any    // page payload: POINTER to a handler-defined struct
}
```

   Pass Data as a pointer and make every page guard with
   `{{with .Data}}` - then rendering with nil Data succeeds, which
   the smoke test below exploits.

2. Typed page structs embedding a shared chrome - the end state
   (`Data any` loses field-name checking at the seam you most need
   it):

```go
type Chrome struct {           // shell data every page embeds
    Title, Error, Flash string
    User    users.User
    Session *users.Session     // nil on pre-auth pages: no app bar
    Admin   bool
}

type HomePage struct {
    Chrome
    Greeting string
    Cards    []Card
}
```

   Embedding flattens fields, so `base.tpl` writes `{{.Title}}` and
   the page writes `{{.Greeting}}` against the same dot. Build the
   Chrome in one helper on the app/kernel struct so every handler
   fills it identically; make chrome lookups soft-fail (log and show
   a zero value) - a nav widget is never worth failing the page for.

## The renderer: buffer first, then status

Never execute a template straight into the ResponseWriter: a failure
halfway leaves the user truncated HTML followed by garbage, with the
200 already sent. Render into a buffer, then commit:

```go
func (r *Renderer) Render(w http.ResponseWriter, status int,
    name string, p *Page) error {
    t, ok := r.pages[name]
    if !ok {
        http.Error(w, "internal error: unknown page", 500)
        return fmt.Errorf("renderer: unknown page %q", name)
    }
    var buf bytes.Buffer
    if err := t.ExecuteTemplate(&buf, "base.tpl", p); err != nil {
        http.Error(w, "internal error rendering page", 500)
        return fmt.Errorf("renderer: execute %q: %w", name, err)
    }
    w.Header().Set("Content-Type", "text/html; charset=utf-8")
    w.Header().Set("Cache-Control", "no-store") // per-user pages
    w.WriteHeader(status)
    _, err := w.Write(buf.Bytes())
    return err
}
```

The caller wraps it once and logs - the user already got a clean 500:

```go
func (g *gui) render(w http.ResponseWriter, status int, name string,
    p *renderer.Page) {
    if err := g.rd.Render(w, status, name, p); err != nil {
        g.log.Error("render failed", "template", name, "err", err)
    }
}
```

Status is a parameter because error pages render with real codes
(403/404/500), not 200. Ship an `error.tpl` and a helper that feeds
it a small `errorData{Status int; Detail string}`, plus a mapper from
domain errors to it (permission → 403 + polite detail, missing → 404,
rest → 500 with a generic detail - never raw internals in HTML).

If a helper does write straight to `w` (acceptable only when pages
are trivial), all it can do on failure is append an HTML comment
(`<!-- render error: %v -->`) - headers are gone. Buffer-first is the
default; direct-write is the documented exception.

## FuncMap conventions

Helpers keep templates declarative: formatting decisions live in Go,
templates only call them. Keep them pure, tiny, lowercase:

```go
var funcs = template.FuncMap{
    "bytes":   humanBytes,        // 1536 → "1.5 KiB"
    "timefmt": timefmt,           // zero time → "-"
    "trunc":   trunc,             // hash → "ab34…"
    "urlq":    url.QueryEscape,   // for building strings, NOT hrefs
    "reltime": Reltime,           // "5m ago" (pair with abstime title)
    "initial": Initial,           // avatar letter, "?" when empty
    "dict":    dict,              // inline maps for partial args
}
```

`dict` lets a partial take several arguments (templates pass one dot):

```go
func dict(pairs ...any) (map[string]any, error) {
    if len(pairs)%2 != 0 { return nil, fmt.Errorf("dict: odd args") }
    m := make(map[string]any, len(pairs)/2)
    for i := 0; i < len(pairs); i += 2 {
        k, ok := pairs[i].(string)
        if !ok {
            return nil, fmt.Errorf("dict: key %v not a string", pairs[i])
        }
        m[k] = pairs[i+1]
    }
    return m, nil
}
```

A `haskey` helper (reflection over a map) is worth having because
`index` cannot distinguish "absent" from "zero value". A helper that
returns `(T, error)` aborts the render on error - good; a helper that
panics kills the whole response - never panic in a FuncMap.

## Autoescaping and the template.HTML boundary

`html/template` escapes by CONTEXT: the same `{{.Key}}` is
HTML-escaped in text, attribute-escaped in an attribute, URL-escaped
inside an `href` query value, JS-escaped inside `<script>`. Two
consequences:

- Never pre-escape values bound for hrefs. `href="/kv?prefix={{.Key}}"`
  is already query-escaped by the engine; wrapping it in `urlq`
  double-encodes ("/" → `%252F`). `urlq` exists for building URL
  strings in ATTRIBUTE-free contexts only.
- To bypass escaping you must mint a typed value - and every mint
  needs a one-line safety proof in a comment:
  - `template.HTML` - only for output that is sanitized (markdown run
    through an HTML sanitizer) or computed entirely from non-user
    data (an SVG sparkline printf'd from integers).
  - `template.CSS` - computed styles, e.g. a gradient derived from a
    hash of the username: every byte comes from integers.
  - `template.URL` - schemes html/template would neuter as
    `#ZgotmplZ`, e.g. `otpauth://` for a TOTP QR link.
  Never `template.HTML(userInput)` - that is the XSS.

`text/template` and `html/template` share syntax but not safety:
render HTML ONLY with `html/template`; use `text/template` for plain
text (emails, config files, shell snippets). The import line is the
security boundary - a code review must treat a `text/template`
rendering into an HTTP HTML response as a bug.

## Embedded static assets and fonts

CSS, JS, fonts, and images embed beside the templates and serve from
memory. No external requests ever - self-host fonts as `.woff2` and
reference them with `@font-face` in the embedded CSS
(`src:url(/static/fonts/inter/inter-latin.woff2) format('woff2')`).

```go
//go:embed tokens.css components.css app.js fonts
var staticFS embed.FS

func Static() http.Handler {
    fileServer := http.FileServer(http.FS(staticFS))
    return http.StripPrefix("/static/", http.HandlerFunc(
        func(w http.ResponseWriter, r *http.Request) {
            w.Header().Set("Cache-Control", AssetCache(r.URL.Path))
            fileServer.ServeHTTP(w, r)
        }))
}

func AssetCache(path string) string {
    switch {
    case strings.HasSuffix(path, ".woff2"), strings.HasSuffix(path, ".ttf"):
        return "public, max-age=31536000, immutable" // content-stable
    case strings.HasSuffix(path, ".css"), strings.HasSuffix(path, ".js"):
        return "no-cache" // stored but revalidated: UI fixes must land
    default:
        return "public, max-age=3600"
    }
}
```

Fonts never change → cache a year. CSS/JS change every build →
`no-cache` (the browser stores but revalidates; a stale stylesheet
after a UI fix reads as "the fix didn't land"). If the embed pattern
is `*`, the asset handler MUST 404 anything ending in `.go` (and
`path.Clean` + reject `..` prefixes for depth) - source is embedded
too and must not be downloadable. Per-app assets follow the same
shape: the app embeds its own `assets` dir and mounts a
StripPrefix'd FileServer under its route prefix, reusing the shared
cache policy.

## Testing the template set

Templates fail at execution, not compile - so make execution a unit
test ([../testing.md](../testing.md)). Four layers:

1. Parse-all: `New()` succeeds and every embedded `.tpl` (minus the
   base) appears in `Names()`. Catches syntax errors at test time.
2. Pin the page set: a literal list of required template names,
   asserted present. A renamed or dropped `.tpl` fails here even
   though glob-driven tests would happily pass with fewer pages.
3. Smoke-render every page with nil payload through the real HTTP
   path - possible because pages guard with `{{with .Data}}`:

```go
func TestRenderEveryPageWithEmptyData(t *testing.T) {
    r := MustNew()
    for _, name := range r.Names() {
        rec := httptest.NewRecorder()
        p := &Page{Title: "t", User: "root", Admin: true, Path: "/"}
        if err := r.Render(rec, 200, name, p); err != nil {
            t.Errorf("Render(%s) = %v", name, err)
            continue
        }
        if !strings.Contains(rec.Body.String(), "<!doctype html>") {
            t.Errorf("Render(%s): missing doctype", name)
        }
    }
}
```

4. Realistic-payload renders, co-located with the handlers: execute
   each page WITH its handler's actual data struct and assert
   substrings - a template referencing a misspelled field only fails
   at execution with THAT struct, so test exactly that pairing.
   Include the escaping proof (`Name: "Grace <Hopper>"` in, expect
   `Grace &lt;Hopper&gt;` out), the empty state, and both branches
   of every `{{if}}` you care about. Also test that an unknown page
   name yields 500 + error, and that error pages carry their real
   status through `Render`.

## Rules

- Parse all templates at startup (`MustNew`/`MustParse`), never per
  request - per-request parsing hides syntax errors until traffic and
  wastes CPU; a parsed `*template.Template` is safe for concurrent
  Execute.
- One template set per page (or per app): `{{define "content"}}` in
  two files of one set silently overrides - the LAST parsed wins, no
  error. Per-page sets make the collision impossible.
- Render to a buffer, then write status + body; `no-store` on
  session-personalized pages.
- `html/template` for HTML, `text/template` for text, no exceptions;
  `template.HTML/CSS/URL` only with a written safety argument.
- Don't hand-escape what the context escaper already handles; `urlq`
  inside an href double-encodes.
- Nav visibility (`.Admin`, `.User`) is cosmetic - the server
  enforces authorization on every route regardless
  ([gorilla-mux.md](gorilla-mux.md)).
- FuncMap helpers are pure formatters; business logic stays in the
  handler/view-model layer ([mvc.md](mvc.md)).
- The CSRF token in the page is the anti-forgery token, never the
  session token - the session cookie stays HttpOnly and out of HTML.
- Embed patterns wider than `*.tpl`/asset extensions require a `.go`
  404 guard in the asset handler.
- Every shipped template is executed by a test with fixture data;
  a template no test renders is a template that breaks in front of a
  user.
