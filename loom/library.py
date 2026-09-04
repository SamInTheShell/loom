"""A library is a folder the user owns - Loom's whole world while open.

Layout (created by create_library, tolerated loosely on open - the only
hard requirement is loom.yaml or loom.yml at the root):

    <library>/
      loom.yaml        the config (models, permissions, containers, chat)
      prompts/         ALL prompts: system, compaction, title (markdown)
      knowledge/       plain-markdown knowledge base, searchable by the model
      containers/      container build files, named by loom.yaml configs
      documentation/   the app docs, seeded once and then user-owned
      internals/       Loom's working data (chats/ transcripts) - hidden
                       in the tree unless "show hidden" is toggled on

New libraries are stamped from a TEMPLATE (built-ins in loom/templates/,
user templates in ~/.loom/templates/) - a template is simply a library
skeleton that gets copied in.

File I/O in this module is sandboxed: every path from the frontend is
resolved against the library root and refused if it escapes it (symlink
targets included - resolve() runs before the containment check).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path


class LibraryError(Exception):
    pass


CONFIG_NAMES = ("loom.yaml", "loom.yml")

# Loom's working data inside a library (chat transcripts). Hidden in the
# tree unless "show hidden" is on; skipped by search always.
INTERNALS = "internals"

# tree/search skip these ALWAYS - noise, never library content
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv"}

BUILTIN_TEMPLATES = Path(__file__).resolve().parent / "templates"
DOCS_DIR = Path(__file__).resolve().parent / "docs"

TEXT_MAX = 4 * 1024 * 1024   # editor refuses files past this - not a doc


def config_path(root: Path) -> Path | None:
    for name in CONFIG_NAMES:
        p = root / name
        if p.is_file():
            return p
    return None


def is_library(path: str | Path) -> bool:
    try:
        return config_path(Path(path).expanduser()) is not None
    except OSError:
        return False


def safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` inside `root` or refuse. The frontend supplies rels;
    they are data, not trusted paths."""
    root = root.resolve()
    p = (root / str(rel or "").lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise LibraryError(f"path escapes the library: {rel!r}")
    return p


# ---------------------------------------------------------------------------
# creation

DEFAULT_COMPACTION_PROMPT = """\
Summarize the conversation so far for your own future context. Keep:
decisions made, constraints stated, open questions, results of tool calls
that still matter, and any file paths or identifiers mentioned. Drop
pleasantries and dead ends. Reply with ONLY the summary.
"""

DEFAULT_TITLE_PROMPT = """\
Name this conversation. Reply with ONLY the title - 2 to 6 words, no
quotes, no trailing punctuation.

If the conversation has a concrete subject, be specific about it. If it is
vague or meandering, invent a short, creative, memorable name instead of a
generic one ("Untitled" and "General chat" are failures). Never duplicate
any of the existing titles you are shown.
"""

DEFAULT_LOOM_YAML = """\
# loom.yaml - this library's configuration. (loom.yml works too.)

# Inference providers - the HTTP APIs Loom talks to. Loom does NOT
# launch inference: run llama.cpp's `llama-server` or `ninfer-serve`
# yourself and point an entry at it. Models are pulled live from each
# provider's API (GET /v1/models).
providers: []
# Complete example - every field:
# - name: workstation           # required - how chats refer to it
#   type: llama-cpp             # llama-cpp | ninfer
#   url: http://127.0.0.1:8080  # required - the API's base URL
#   ssh: ""                     # default "" = connect directly; else an
#                               # ssh destination - the url is resolved
#                               # FROM that host over an ssh stdio
#                               # tunnel. SSH KEYS ONLY.

chat:
  # default provider (by name) for new chats; empty = the first one
  provider: ""
  # default model id for new chats; empty = the provider's first model
  model: ""
  # default permission mode for new chats (pick per chat in the composer)
  permission_mode: always-ask
  system_prompt: prompts/system.md
  compaction_prompt: prompts/compaction.md
  title_prompt: prompts/title.md
  # older assistant thoughts are dropped from the context; only the most
  # recent turn's thinking is carried forward
  thought_truncation: true
  compaction:
    auto: true          # compact automatically when the context fills up
    threshold: 0.8      # fraction of the context window that triggers it

# Permission MODES - a chat runs under one mode; switch it in the chat's
# input box. These tables are the ACTUAL levels chats run under - edit
# them freely, or add brand-new modes. A tool left out of a table falls
# back to that built-in mode's default.
# Levels: allow / ask / deny / disabled (disabled = tool not offered).
permission-modes:
  always-ask:          # reads allowed, changes and shell always confirm
    tools:
      knowledge_search: allow
      read_file: allow
      list_dir: allow
      grep: allow
      find_files: allow
      write_file: ask
      edit_file: ask
      shell: ask
  allow-edits:         # edits and sandboxed shell run without asking
    tools:
      knowledge_search: allow
      read_file: allow
      list_dir: allow
      grep: allow
      find_files: allow
      write_file: allow
      edit_file: allow
      shell: allow
  always-allow:        # everything runs without asking
    tools:
      knowledge_search: allow
      read_file: allow
      list_dir: allow
      grep: allow
      find_files: allow
      write_file: allow
      edit_file: allow
      shell: allow
  # read-only:         # example custom mode
  #   tools:
  #     write_file: disabled
  #     edit_file: disabled
  #     shell: disabled

# shell commands from chats run inside a container (non-root user),
# never directly on the host.
containers:
  engine: auto          # auto | podman | docker
  default: sandbox
  definitions:
  - name: sandbox
    file: containers/sandbox.Containerfile
"""


# ---------------------------------------------------------------------------
# templates: a template is a library skeleton that gets copied in. Built-ins
# ship in loom/templates/<id>/; user templates live in ~/.loom/templates/<id>/.
# An optional template.txt (one line) is the picker description - not copied.

def user_templates_dir() -> Path:
    from loom import store
    return store.home() / "templates"


def list_templates() -> list[dict]:
    out = []

    def scan(base: Path, builtin: bool):
        if not base.is_dir():
            return
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            desc = ""
            try:
                desc = (d / "template.txt").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            out.append({"id": d.name,
                        "name": d.name.replace("-", " ").replace("_", " ").title(),
                        "description": desc.splitlines()[0] if desc else "",
                        "builtin": builtin})
    scan(BUILTIN_TEMPLATES, True)
    scan(user_templates_dir(), False)
    return out


def _template_dir(template_id: str) -> Path:
    tid = str(template_id or "starter")
    if "/" in tid or "\\" in tid or tid.startswith("."):
        raise LibraryError(f"bad template id: {template_id!r}")
    for base in (BUILTIN_TEMPLATES, user_templates_dir()):
        d = base / tid
        if d.is_dir():
            return d
    raise LibraryError(f"no such template: {template_id!r}")


def _copy_template(src: Path, root: Path) -> None:
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if rel.parts and rel.parts[0] == "template.txt":
            continue
        dst = root / rel
        if p.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)


def ensure_documentation(root: Path) -> None:
    """Seed the app documentation once. Only when documentation/ is absent
    entirely - a user's edited docs are never overwritten."""
    dst = root / "documentation"
    if dst.exists() or not DOCS_DIR.is_dir():
        return
    dst.mkdir(parents=True)
    for p in sorted(DOCS_DIR.glob("*.md")):
        shutil.copy2(p, dst / p.name)


def _migrate_layout(root: Path) -> None:
    """Older libraries kept chats at ./chats - they now live under
    ./internals/chats. Move transcripts, never overwrite."""
    legacy = root / "chats"
    if not legacy.is_dir():
        return
    dst = root / INTERNALS / "chats"
    dst.mkdir(parents=True, exist_ok=True)
    for p in legacy.glob("*.json*"):
        target = dst / p.name
        if not target.exists():
            try:
                p.rename(target)
            except OSError:
                pass
    try:
        legacy.rmdir()   # only removes if now empty
    except OSError:
        pass


def create_library(path: str, template: str = "starter") -> Path:
    """Scaffold a new library at `path` from a template (created if
    missing; must be empty or already a library - never silently adopt a
    random full folder)."""
    root = Path(path).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if config_path(root) is not None:
        _migrate_layout(root)
        ensure_documentation(root)
        return root   # already a library - opening it is the right move
    if any(root.iterdir()):
        raise LibraryError(
            f"{root} is not empty and not a library - pick an empty folder "
            "or an existing library")
    _copy_template(_template_dir(template), root)
    (root / INTERNALS / "chats").mkdir(parents=True, exist_ok=True)
    ensure_documentation(root)
    if config_path(root) is None:   # a user template without a loom.yaml
        (root / "loom.yaml").write_text(DEFAULT_LOOM_YAML, encoding="utf-8")
    return root


def open_library(path: str) -> Path:
    root = Path(path).expanduser()
    if not root.is_dir():
        raise LibraryError(f"not a folder: {path}")
    if config_path(root) is None:
        raise LibraryError(
            f"{path} has no loom.yaml (or loom.yml) - not a library. "
            "Use Create to scaffold one.")
    root = root.resolve()
    _migrate_layout(root)
    ensure_documentation(root)
    return root


# ---------------------------------------------------------------------------
# the navigation tree + file I/O (Library tab)

def tree(root: Path, show_hidden: bool = False) -> list[dict]:
    """Nested listing: [{name, rel, dir, children?}], dirs first, sorted.
    Hidden entries - dotfiles and the root-level internals/ dir - only
    appear when show_hidden (the tree's right-click toggle) is on."""
    def walk(d: Path, rel: str) -> list[dict]:
        out = []
        try:
            entries = sorted(d.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return out
        for p in entries:
            hidden = p.name.startswith(".") \
                or (rel == "" and p.name == INTERNALS)
            if hidden and not show_hidden:
                continue
            if p.is_dir() and p.name in SKIP_DIRS:
                continue
            r = f"{rel}/{p.name}" if rel else p.name
            if p.is_dir():
                out.append({"name": p.name, "rel": r, "dir": True,
                            "hidden": hidden, "children": walk(p, r)})
            else:
                out.append({"name": p.name, "rel": r, "dir": False,
                            "hidden": hidden})
        return out
    return walk(root, "")


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def read_file(root: Path, rel: str) -> dict:
    p = safe_join(root, rel)
    if not p.is_file():
        raise LibraryError(f"no such file: {rel}")
    if p.stat().st_size > TEXT_MAX:
        raise LibraryError(f"{rel} is over {TEXT_MAX // (1024*1024)} MB - "
                           "too large for the editor")
    data = p.read_bytes()
    if looks_binary(data):
        return {"rel": rel, "binary": True, "size": len(data)}
    return {"rel": rel, "binary": False,
            "text": data.decode("utf-8", "replace"),
            "mtime": int(p.stat().st_mtime * 1000)}


def write_file(root: Path, rel: str, text: str) -> dict:
    p = safe_join(root, rel)
    if p.is_dir():
        raise LibraryError(f"{rel} is a folder")
    p.parent.mkdir(parents=True, exist_ok=True)
    # pid AND thread - concurrent writers in one process must never
    # share a tmp file (store.py sets the pattern)
    tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(str(text), encoding="utf-8")
        tmp.replace(p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"rel": rel, "mtime": int(p.stat().st_mtime * 1000)}


def create_entry(root: Path, rel: str, directory: bool) -> dict:
    p = safe_join(root, rel)
    if p.exists():
        raise LibraryError(f"already exists: {rel}")
    if directory:
        p.mkdir(parents=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    return {"rel": rel}


def rename_entry(root: Path, rel: str, new_rel: str) -> dict:
    src = safe_join(root, rel)
    dst = safe_join(root, new_rel)
    if not src.exists():
        raise LibraryError(f"no such entry: {rel}")
    if dst.exists():
        raise LibraryError(f"already exists: {new_rel}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"rel": new_rel}


def delete_entry(root: Path, rel: str) -> dict:
    p = safe_join(root, rel)
    if p == root.resolve():
        raise LibraryError("refusing to delete the library root")
    if not p.exists():
        raise LibraryError(f"no such entry: {rel}")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"rel": rel, "deleted": True, "ts": int(time.time() * 1000)}


PID_FILE = ".loom-pid"


def _lock_holder_alive(pid: int) -> bool:
    """Does the pid in a library's lock file still look like a live Loom?
    Dead pid → stale lock; live pid that clearly is not Loom → pid reuse,
    also stale; uninspectable → assume the lock is honest (a false
    "alive" merely refuses the open; a false "stale" would let two
    instances share a library - always err toward alive)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)   # signal 0 = existence probe
    except ProcessLookupError:
        return False
    except PermissionError:
        pass   # alive but not ours - the checks below still apply
    # the executable must be plausible for Loom (python/uv/loom) - this
    # rules out a recycled pid whose CMDLINE merely mentions a loom path
    # (an editor open on ~/projects/loom is not a Loom instance)
    try:
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe")).lower()
        if exe and not (exe.startswith("python") or exe in ("loom", "uv")):
            return False
    except OSError:
        pass   # unreadable exe (perms) - fall through to the cmdline
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes() \
            .decode("utf-8", "replace").lower()
        return "loom" in cmd
    except OSError:
        return True


def acquire_lock(root: Path) -> None:
    """Claim a library for THIS process via .loom-pid at its root. Two
    Loom instances must never consume the same library - the second one
    is refused with a pointer at the first. A stale lock (crashed or
    recycled pid) is taken over silently."""
    p = root / PID_FILE
    if p.is_file():
        try:
            pid = int(p.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _lock_holder_alive(pid):
            raise LibraryError(
                f"this library is already open in another Loom instance "
                f"(pid {pid}) - close it there first; two instances may "
                "not share a library")
    p.write_text(str(os.getpid()), encoding="utf-8")


def release_lock(root: Path | None) -> None:
    """Drop our claim - only ever our OWN: a lock another live instance
    holds is never deleted."""
    if root is None:
        return
    p = Path(root) / PID_FILE
    try:
        if p.is_file() and p.read_text(encoding="utf-8").strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


def git_branch(path: Path) -> str | None:
    """The checked-out branch of a folder, or a short sha when detached,
    or None when it isn't a git worktree. Reads .git/HEAD directly - no
    subprocess - so it's cheap enough to poll for the attachment pills."""
    try:
        gd = Path(path) / ".git"
        if gd.is_file():   # worktree / submodule pointer file
            txt = gd.read_text(encoding="utf-8", errors="replace")
            line = next((ln for ln in txt.splitlines()
                         if ln.startswith("gitdir:")), "")
            target = line[len("gitdir:"):].strip()
            if not target:
                return None
            gd = (Path(path) / target).resolve()
        head = gd / "HEAD"
        if not head.is_file():
            return None
        txt = head.read_text(encoding="utf-8", errors="replace").strip()
        if txt.startswith("ref:"):
            ref = txt.split(None, 1)[1] if len(txt.split(None, 1)) > 1 else ""
            for prefix in ("refs/heads/", "refs/"):
                if ref.startswith(prefix):
                    return ref[len(prefix):] or None
            return ref or None
        return txt[:7] or None   # detached HEAD → short sha
    except OSError:
        return None
