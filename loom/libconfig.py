"""loom.yaml - the library's configuration, parsed fresh on every read.

The file is user-owned and hand-edited (in the Library tab); this module
never writes it. Parsing is deliberately forgiving about absent sections
and strict about shape errors.

Loom does NOT launch inference. It talks to inference servers you run
yourself, over their HTTP APIs - llama.cpp's llama-server and ninfer -
directly, or through an SSH tunnel (key auth only):

    providers:
    - name: workstation          # how this endpoint shows up in the UI
      type: llama-cpp            # llama-cpp | ninfer
      url: http://127.0.0.1:8080 # the API's base URL
      ssh: ""                    # optional ssh destination - the url is
                                 # then resolved FROM that host, and all
                                 # traffic rides an ssh stdio tunnel
    - name: gpu-box
      type: ninfer
      url: http://127.0.0.1:11434
      ssh: sam@gpu-box

    chat:
      provider: workstation      # default provider for new chats
      model: qwen3-coder         # default model id (as listed by the API)

Models are pulled live from each provider's API - nothing about model
files, flags, or process management lives here."""

from __future__ import annotations

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


PROVIDER_TYPES = ("llama-cpp", "ninfer")


def load(root: Path) -> dict:
    """Parse the library's loom.yaml/yml into
    {providers, chat, permissions, containers}. Raises ConfigError with a
    message worth showing the user."""
    p = library.config_path(root)
    if p is None:
        raise ConfigError("the library has no loom.yaml (or loom.yml)")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {p.name}: {e}")
    return parse_text(text, p.name)


def parse_text(text: str, name: str = "loom.yaml") -> dict:
    """Parse and validate loom.yaml CONTENT (the whole file). Config
    writers run candidates through this before anything lands on disk."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{name} does not parse: {e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping at the top level")
    if raw.get("models") and not raw.get("providers"):
        raise ConfigError(
            "this loom.yaml uses the old `models:` scheme - Loom no "
            "longer launches llama-server. Run your inference server "
            "yourself and define `providers:` entries pointing at its "
            "HTTP API (see documentation/models-servers.md)")

    out = {
        "configFile": name,
        "providers": _providers(raw.get("providers")),
        "chat": _chat(raw.get("chat")),
        "permissionModes": _permission_modes(
            raw.get("permission-modes", raw.get("permission_modes")),
            raw.get("permissions")),
        "containers": _containers(raw.get("containers")),
        "api": _api(raw.get("api")),
        "mcpServers": _mcp_servers(raw.get("mcp-servers",
                                           raw.get("mcp_servers"))),
    }
    if out["chat"]["permission_mode"] not in out["permissionModes"]:
        raise ConfigError(
            f"chat.permission_mode {out['chat']['permission_mode']!r} is not "
            "a defined permission mode")
    return out


def _providers(sec) -> list[dict]:
    if sec is None:
        return []
    if not isinstance(sec, list):
        raise ConfigError("`providers` must be a list")
    out, names = [], set()
    for i, m in enumerate(sec):
        if not isinstance(m, dict):
            raise ConfigError(f"providers[{i}] must be a mapping")
        name = str(m.get("name") or "").strip()
        if not name:
            raise ConfigError(f"providers[{i}] has no name")
        if name in names:
            raise ConfigError(f"two providers share the name {name!r}")
        names.add(name)
        ptype = str(m.get("type") or "llama-cpp").strip().lower()
        if ptype not in PROVIDER_TYPES:
            raise ConfigError(
                f"provider {name!r}: type must be one of "
                + ", ".join(PROVIDER_TYPES) + f" - not {ptype!r}")
        url = str(m.get("url") or "").strip().rstrip("/")
        if not url:
            raise ConfigError(f"provider {name!r} has no `url`")
        if not re.match(r"^https?://", url):
            raise ConfigError(
                f"provider {name!r}: url must start with http:// or "
                f"https:// - got {url!r}")
        ssh = str(m.get("ssh") or "").strip()
        if ssh.startswith("-"):
            raise ConfigError(
                f"provider {name!r}: ssh must be a destination "
                "(user@host or a ~/.ssh/config alias), not flags")
        out.append({"name": name, "type": ptype, "url": url, "ssh": ssh})
    return out


def provider_by_name(cfg: dict, name: str) -> dict | None:
    provs = cfg.get("providers") or []
    return next((p for p in provs if p["name"] == name), None) \
        or (provs[0] if provs and not name else None)


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
        "provider": str(sec.get("provider") or "").strip(),
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
        # assistant replies carry their own datetime signal in [brackets]
        # on the wire (set false to strip every assistant time signal)
        "assistant_signals": bool(sec.get("assistant_signals", True)),
        # the agent's display name in the chat UI (the word whose hover
        # reveals each reply's signals)
        "assistant_name": str(sec.get("assistant_name")
                              or "loom").strip() or "loom",
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


DEFAULT_API = {"interface": "127.0.0.1", "port": 1234}


def _api(sec) -> dict:
    """`api:` - where the OpenAI-compatible API binds when toggled on.
    Only interface + port persist here; on/off is process state (always
    off at launch)."""
    sec = sec if isinstance(sec, dict) else {}
    iface = str(sec.get("interface")
                or DEFAULT_API["interface"]).strip()
    try:
        port = int(sec.get("port", DEFAULT_API["port"]))
    except (TypeError, ValueError):
        raise ConfigError("api.port must be a number")
    if not (1 <= port <= 65535):
        raise ConfigError("api.port must be 1-65535")
    return {"interface": iface, "port": port}


def _mcp_servers(sec) -> list[dict]:
    """`mcp-servers:` - stdio MCP servers the chats may call.

        mcp-servers:
        - name: files
          command: npx -y @modelcontextprotocol/server-filesystem /tmp
          env:
            FOO: bar

    Tool names surface to the model (and to permission-modes.<mode>.tools)
    as mcp_<server>_<tool>."""
    if sec is None:
        return []
    if not isinstance(sec, list):
        raise ConfigError("mcp-servers must be a list")
    out, names = [], set()
    for i, s in enumerate(sec):
        if not isinstance(s, dict):
            raise ConfigError(f"mcp-servers[{i}] must be a mapping")
        name = str(s.get("name") or "").strip()
        if not name:
            raise ConfigError(f"mcp-servers[{i}] has no name")
        if not re.match(r"^[\w-]+$", name):
            raise ConfigError(
                f"mcp-servers[{i}]: name {name!r} - letters, digits, - and _ "
                "only (it becomes part of tool function names)")
        if name in names:
            raise ConfigError(f"two mcp servers share the name {name!r}")
        names.add(name)
        command = str(s.get("command") or "").strip()
        if not command:
            raise ConfigError(f"mcp server {name!r} has no `command`")
        env = s.get("env")
        if env is not None and not isinstance(env, dict):
            raise ConfigError(f"mcp server {name!r}: env must be a mapping")
        out.append({"name": name, "command": command,
                    "env": {str(k): str(v) for k, v in (env or {}).items()}})
    return out


def permission_for(cfg: dict, tool: str, mode: str = "") -> str:
    """The level for `tool` under `mode` (falls back to the config's
    default mode, then always-ask; unknown tools default to ask)."""
    modes = cfg.get("permissionModes") or BUILTIN_MODES
    name = mode or (cfg.get("chat") or {}).get("permission_mode") or DEFAULT_MODE
    tools = modes.get(name) or modes.get(DEFAULT_MODE) \
        or BUILTIN_MODES[DEFAULT_MODE]
    return tools.get(tool, "ask")
