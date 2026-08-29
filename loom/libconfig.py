"""loom.yaml — the library's configuration, parsed fresh on every read.

The file is user-owned and hand-edited (in the Library tab); this module
never writes it. Parsing is deliberately forgiving about absent sections
and strict about shape errors, and every model entry gets a DETERMINISTIC
id (slug of name + short hash of name+ssh) so srv.py's on-host state dirs
stay stable across app restarts and yaml edits that don't rename.

    models:
    - name: Qwen 3.8 27B 128k
      ssh: ""            # ssh destination; empty = this machine
      context: 128000
      model: ~/.lmstudio/models/.../Qwen3.8-27B-Q4_K_M.gguf
      mmproj: ~/.lmstudio/models/.../mmproj-Qwen3.8-27B-BF16.gguf
      binary: llama-server   # optional
      flags: |
        -ngl 99
        -fa on
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from loom import library


class ConfigError(Exception):
    pass


PERM_LEVELS = ("allow", "ask", "deny", "disabled")

READ_TOOLS = ("knowledge_search", "read_file", "list_dir", "grep", "find_files")
EDIT_TOOLS = ("write_file", "edit_file")

# Built-in permission MODES. A chat runs under exactly one mode; loom.yaml
# `permission-modes` entries override/extend these per tool, and may define
# entirely new modes. Levels: allow / ask / deny / disabled (disabled =
# the tool is not even offered to the model).
BUILTIN_MODES = {
    "always-ask": {
        **{t: "allow" for t in READ_TOOLS},
        **{t: "ask" for t in EDIT_TOOLS},
        "shell": "ask",
    },
    # shell is ALLOWED here: the container enforces the boundary (view
    # mounts ro, network off unless the user flips the chip), so a shell
    # command has no more write power than the edit tools this mode
    # already allows
    "allow-edits": {
        **{t: "allow" for t in READ_TOOLS},
        **{t: "allow" for t in EDIT_TOOLS},
        "shell": "allow",
    },
    "always-allow": {
        **{t: "allow" for t in READ_TOOLS},
        **{t: "allow" for t in EDIT_TOOLS},
        "shell": "allow",
    },
}
DEFAULT_MODE = "always-ask"

DEFAULT_CONTAINERS = {
    "engine": "auto",
    "default": "sandbox",
    "definitions": [],   # [{name, file}]
}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "model"
    return s[:32]


def model_id(name: str, ssh: str) -> str:
    h = hashlib.sha256(f"{name}\x00{ssh}".encode()).hexdigest()[:8]
    return f"{_slug(name)}-{h}"


def load(root: Path) -> dict:
    """Parse the library's loom.yaml/yml into
    {models, chat, permissions, containers}. Raises ConfigError with a
    message worth showing the user."""
    p = library.config_path(root)
    if p is None:
        raise ConfigError("the library has no loom.yaml (or loom.yml)")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p.name} does not parse: {e}")
    except OSError as e:
        raise ConfigError(f"cannot read {p.name}: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{p.name} must be a mapping at the top level")

    out = {
        "configFile": p.name,
        "models": _models(raw.get("models")),
        "chat": _chat(raw.get("chat")),
        "permissionModes": _permission_modes(
            raw.get("permission-modes", raw.get("permission_modes")),
            raw.get("permissions")),
        "containers": _containers(raw.get("containers")),
    }
    if out["chat"]["permission_mode"] not in out["permissionModes"]:
        raise ConfigError(
            f"chat.permission_mode {out['chat']['permission_mode']!r} is not "
            "a defined permission mode")
    return out


def _models(sec) -> list[dict]:
    if sec is None:
        return []
    if not isinstance(sec, list):
        raise ConfigError("`models` must be a list")
    out, names = [], set()
    for i, m in enumerate(sec):
        if not isinstance(m, dict):
            raise ConfigError(f"models[{i}] must be a mapping")
        name = str(m.get("name") or "").strip()
        if not name:
            raise ConfigError(f"models[{i}] has no name")
        if name in names:
            raise ConfigError(f"two models share the name {name!r}")
        names.add(name)
        model = str(m.get("model") or "").strip()
        if not model:
            raise ConfigError(f"model {name!r} has no `model` (GGUF path)")
        ssh = str(m.get("ssh") or "").strip()
        ctx = m.get("context", 0)
        try:
            ctx = max(0, int(ctx or 0))
        except (TypeError, ValueError):
            raise ConfigError(f"model {name!r}: context must be a number")
        out.append({
            "id": model_id(name, ssh),
            "name": name,
            # srv.py's vocabulary: host = ssh destination, ctx = context
            "host": ssh,
            "model": model,
            "mmproj": str(m.get("mmproj") or "").strip(),
            "ctx": ctx,
            "flags": str(m.get("flags") or ""),
            "serverBin": str(m.get("binary") or "").strip(),
        })
    return out


def _chat(sec) -> dict:
    sec = sec if isinstance(sec, dict) else {}
    comp = sec.get("compaction")
    comp = comp if isinstance(comp, dict) else {}
    try:
        threshold = float(comp.get("threshold", 0.8))
    except (TypeError, ValueError):
        raise ConfigError("chat.compaction.threshold must be a number "
                          "(fraction of the context window, e.g. 0.8)")
    if not (0.2 <= threshold <= 0.95):
        raise ConfigError("chat.compaction.threshold must be between "
                          "0.2 and 0.95")
    return {
        "model": str(sec.get("model") or "").strip(),
        "permission_mode": str(sec.get("permission_mode")
                               or DEFAULT_MODE).strip(),
        "system_prompt": str(sec.get("system_prompt")
                             or "prompts/system.md").strip(),
        "compaction_prompt": str(sec.get("compaction_prompt")
                                 or "prompts/compaction.md").strip(),
        "title_prompt": str(sec.get("title_prompt")
                            or "prompts/title.md").strip(),
        # older thoughts are dropped from the wire; the LAST turn's
        # thinking is kept (set false to keep every thought)
        "thought_truncation": bool(sec.get("thought_truncation", True)),
        "compaction": {"auto": bool(comp.get("auto", True)),
                       "threshold": threshold},
    }


def _permission_modes(sec, legacy) -> dict:
    """`permission-modes` → {name: {tool: level}}. Built-in modes are always
    present; yaml entries override per tool or define new modes. The legacy
    `permissions: {tools: {...}}` shape is honored as overrides to
    always-ask so old configs keep working."""
    modes = {name: dict(tools) for name, tools in BUILTIN_MODES.items()}

    def apply(mode_name, tools, where):
        if not isinstance(tools, dict):
            raise ConfigError(f"{where} must be a mapping of tool: level")
        base = modes.setdefault(mode_name, dict(BUILTIN_MODES[DEFAULT_MODE]))
        for k, v in tools.items():
            lv = str(v or "").strip().lower()
            if lv not in PERM_LEVELS:
                raise ConfigError(
                    f"{where}.{k} must be one of {PERM_LEVELS}, not {v!r}")
            base[str(k)] = lv

    if isinstance(legacy, dict) and isinstance(legacy.get("tools"), dict):
        apply(DEFAULT_MODE, legacy["tools"], "permissions.tools")
    if sec is not None:
        if not isinstance(sec, dict):
            raise ConfigError("permission-modes must be a mapping")
        for name, body in sec.items():
            if body is None:
                modes.setdefault(str(name), dict(BUILTIN_MODES[DEFAULT_MODE]))
                continue
            if not isinstance(body, dict):
                raise ConfigError(f"permission-modes.{name} must be a mapping")
            apply(str(name), body.get("tools") or {},
                  f"permission-modes.{name}.tools")
    return modes


def _containers(sec) -> dict:
    sec = sec if isinstance(sec, dict) else {}
    engine = str(sec.get("engine") or "auto").strip().lower()
    if engine not in ("auto", "podman", "docker"):
        raise ConfigError("containers.engine must be auto, podman, or docker")
    defs = []
    got = sec.get("definitions")
    if got is not None:
        if not isinstance(got, list):
            raise ConfigError("containers.definitions must be a list")
        for i, d in enumerate(got):
            if not isinstance(d, dict) or not d.get("name"):
                raise ConfigError(f"containers.definitions[{i}] needs a name")
            defs.append({"name": str(d["name"]).strip(),
                         "file": str(d.get("file") or "").strip()})
    return {"engine": engine,
            "default": str(sec.get("default") or DEFAULT_CONTAINERS["default"]).strip(),
            "definitions": defs}


def permission_for(cfg: dict, tool: str, mode: str = "") -> str:
    """The level for `tool` under `mode` (falls back to the config's
    default mode, then always-ask; unknown tools default to ask)."""
    modes = cfg.get("permissionModes") or BUILTIN_MODES
    name = mode or (cfg.get("chat") or {}).get("permission_mode") or DEFAULT_MODE
    tools = modes.get(name) or modes.get(DEFAULT_MODE) \
        or BUILTIN_MODES[DEFAULT_MODE]
    return tools.get(tool, "ask")
