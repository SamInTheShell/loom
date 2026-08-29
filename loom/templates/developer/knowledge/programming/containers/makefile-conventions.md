# Makefile conventions

The Makefile is the front door of a repo: a new contributor (human or
model) types `make` and learns everything the project can do. These
conventions make that true with zero external tooling.

## Rule of thumb: `make` alone prints help

Every Makefile defaults to a help target — never to a build. Building on
a bare `make` surprises people; help never does.

```make
.DEFAULT_GOAL := help

.PHONY: help build test clean   # every target; there is no file tracking

help: ## show this help
	@echo 'Usage: make <target>'
	@echo ''
	@echo '  build   compile into ./bin'
	@echo '  test    run the test suite'
	@echo '  clean   remove build artifacts'
```

Annotate real targets with a trailing `## one-line description` — the
same line the help text explains. Keep the two in sync when you add a
target; the help IS the documentation of record.

Mark all targets `.PHONY` when the toolchain (Go, uv, npm) already does
its own incremental builds — make-level file tracking then only causes
stale-target bugs.

## Auto-detect podman vs docker (and let the user override)

Never hard-code the container engine. Prefer podman when installed,
fall back to docker:

```make
DOCKER_CMD := $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
```

Every container recipe then uses `$(DOCKER_CMD) build`, `$(DOCKER_CMD)
run`, … Overriding is standard make: any variable set on the command
line beats the file —

```sh
make docker DOCKER_CMD=docker     # force docker on a podman machine
```

For values users override routinely (image names, cluster names,
ports), declare them with `?=` so an environment variable works too:

```make
IMAGE ?= localhost/myapp:dev
KIND_CLUSTER ?= myapp
```

## The `localhost/` image-tag rule

Always fully qualify local image tags as `localhost/<name>:<tag>`.
Podman qualifies bare tags as `localhost/<name>` while docker leaves
them bare — using the fully-qualified form everywhere is what keeps
`kind load`, `save`, and retagging behaving identically under both
engines.

## Build metadata that degrades gracefully

Inject version info at build time, with fallbacks so a tarball checkout
(no `.git`) still builds:

```make
VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT  := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

docker: ## build the image with the detected runtime
	$(DOCKER_CMD) build --build-arg VERSION=$(VERSION) -t $(IMAGE) .
```
