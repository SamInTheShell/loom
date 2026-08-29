# uv projects in Makefiles — build, install, publish

The Makefile conventions (help by default, self-documenting `##`
targets — see programming/containers/makefile-conventions.md) apply to
Python projects too; the recipes just call uv. The standard block for a
uv-managed project:

```make
.DEFAULT_GOAL := help
.PHONY: help test build install publish clean

help: ## show this help
	@echo 'Usage: make <target>'
	@echo ''
	@echo '  test     run the test suite'
	@echo '  build    build sdist + wheel into ./dist'
	@echo '  install  install this tool for the current user (uv tool)'
	@echo '  publish  build fresh and upload to PyPI'
	@echo '  clean    remove build artifacts'

test: ## run the test suite
	uv run pytest            # or the project's script-style runner

build: ## build sdist + wheel into ./dist
	uv build

install: ## install the CLI for the current user, replacing any old copy
	uv tool install --force .

publish: clean build ## build FRESH artifacts and upload them to PyPI
	uv publish

clean: ## remove build artifacts
	rm -rf dist build *.egg-info
```

## The targets, and their sharp edges

- **build** — `uv build` produces both the sdist and the wheel in
  `dist/`. It needs a `[build-system]` in pyproject.toml; projects that
  deliberately run from source (no build-system) simply don't get
  build/publish targets — don't add packaging just to have them.
- **install** — for a project that ships a CLI, `uv tool install
  --force .` puts the entry points on the user's PATH in an isolated
  env; `--force` makes the target idempotent (re-running upgrades the
  installed copy instead of erroring). For libraries there is nothing
  to "install" — developers `uv add` them; skip the target.
- **publish** — always `clean build` first: `uv publish` uploads
  whatever sits in `dist/`, and a stale wheel from last week is the
  classic wrong-version release. Auth comes from the environment —
  `UV_PUBLISH_TOKEN` (a PyPI API token) — never hard-coded in the
  Makefile; in Loom, that's exactly what a secret stub in an
  environment is for. Remember PyPI versions are immutable: a botched
  upload burns the version number, so bump `version` in pyproject.toml
  before publishing (metadata edits by hand are fine — only
  dependencies must go through `uv add`).

## Version discipline

`uv publish` will happily re-fail on an already-used version. The
pre-publish checklist is three lines: bump `version =` in
pyproject.toml, `make test`, `make publish`. Tag the commit afterwards
so `git describe` and the published version agree.
