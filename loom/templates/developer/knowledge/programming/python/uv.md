# uv — Python package and project manager

uv manages Python versions, virtualenvs, dependencies, and script running
for a project, driven by `pyproject.toml` and a `uv.lock` lockfile.

## The one rule: `uv add`, never hand-edit dependencies

Add and remove dependencies with the CLI:

```bash
uv add requests             # add a dependency
uv add 'pywebview>=6.2.1'   # add with a version constraint
uv add --dev pytest         # add a development-only dependency
uv remove requests          # remove one
```

Do **not** edit the `dependencies` list in `pyproject.toml` by hand.
`uv add` does three things in one atomic step that a manual edit skips:

1. Resolves the version against everything already installed and writes a
   working constraint into `pyproject.toml`.
2. Updates `uv.lock` so every machine gets the exact same versions.
3. Syncs the project virtualenv so the package is importable immediately.

A hand-edited toml leaves the lockfile and the environment stale, which
surfaces later as "works on my machine" or import errors. Editing
`pyproject.toml` directly is fine for everything that is NOT a dependency:
project metadata, scripts/entry points, tool configuration sections.

## Everyday commands

```bash
uv run <command>            # run inside the project env (e.g. uv run pytest)
uv run python -c "..."      # quick smoke test of a module
uv run python script.py     # run a script with the project's deps
uv sync                     # make the env match uv.lock exactly
uv lock --upgrade           # re-resolve everything to newest allowed versions
uv add --upgrade <pkg>      # upgrade one package (bumps constraint if needed)
uv python install 3.12      # install a Python version
uv python pin 3.12          # pin it for this project (.python-version)
uv init                     # start a new project (creates pyproject.toml)
```

Prefer `uv run <cmd>` over activating the virtualenv manually — it
guarantees the env is synced with the lockfile before running, so you can
never run against stale dependencies.

## Reading a uv project

- `pyproject.toml` — declared dependencies (constraints, not exact pins)
  and project metadata. Entry points live under `[project.scripts]`.
- `uv.lock` — the exact resolved versions; machine-generated, never edit,
  always commit.
- `.python-version` — the pinned interpreter version for the project.
- `.venv/` — the project environment; disposable, `uv sync` rebuilds it.

## Gotchas

- After pulling changes that touch `pyproject.toml` or `uv.lock`, run
  `uv sync` (or just use `uv run`, which syncs automatically).
- `uv pip install <pkg>` installs WITHOUT updating `pyproject.toml` or the
  lockfile — the dependency silently disappears for everyone else. In a uv
  project, always use `uv add` instead.
- Constraints in `pyproject.toml` say what versions are acceptable;
  `uv.lock` says what is actually installed. Committing one without the
  other is how environments drift.
