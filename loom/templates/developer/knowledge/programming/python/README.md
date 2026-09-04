# Python

Python knowledge: project tooling first, then libraries (one folder per
library).

- **uv.md** - managing Python projects with uv. Read this before touching
  any `pyproject.toml`: dependencies are managed with `uv add` /
  `uv remove`, not by editing the file by hand.
- **uv-make-targets.md** - the standard Makefile block for uv projects:
  test / build / install / publish (PyPI) targets, uv tool installs,
  token handling, and version discipline before publishing.
- **pywebview/** - building desktop GUI apps with pywebview on the Qt
  backend: the app skeleton, the Python↔JavaScript bridge, local
  DevTools, and debugging workflows.
- **agent-harness/** - the component catalog for a desktop AI coding
  workbench: the agentic tool-calling loop, streaming (and the
  streaming cursor), LLM provider adapters and routing, a bare-repo
  git depot with worktree-less commits, container-sandboxed shell
  execution, agent tool catalogs, keyring-backed secret envs, hybrid
  FTS5+vector search, attachment extraction, the harness-grade UI
  bridge, and persistence. Start with its README - it is the assembly
  map. The matching frontend techniques live in `../web/`.
