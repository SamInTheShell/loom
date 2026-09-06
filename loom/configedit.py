"""Targeted, comment-preserving edits to loom.yaml TEXT.

loom.yaml is user-owned - hand-written comments and ordering outside the
touched block must survive byte-for-byte. Nothing here re-dumps the file;
every edit is a line splice in the spirit of providers.inject_provider and
apiserver.inject_api_config. Callers validate the RESULT with
libconfig.parse_text before anything lands on disk."""

from __future__ import annotations

import re

import yaml

from loom import libconfig


def yq(s: str) -> str:
    """Quote a scalar for YAML when it needs it."""
    s = str(s)
    if s == "":
        return '""'
    if re.search(r"[:#{}\[\],&*?|>'\"%@`!]", s) or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _alt(keys) -> str:
    return "|".join(re.escape(k) for k in keys)


def _block_key_re(keys) -> re.Pattern:
    # `key:` alone, or with an empty flow list/map, or a trailing comment
    return re.compile(
        rf"^(?:{_alt(keys)}):\s*(\[\s*\]\s*|\{{\s*\}}\s*)?(#.*)?$")


# ---------------------------------------------------------------------------
# whole-section replace (chat / permission-modes / containers / permissions)

def replace_section(text: str, keys, lines) -> str:
    """Replace one top-level section of loom.yaml TEXT with `lines` (the
    full new block, `key:` header included). Empty `lines` removes the
    section; a missing section is appended at the end. Blank/comment
    padding around the block keeps its place; comments INSIDE the replaced
    block go with it. `keys` lists the key's accepted spellings."""
    src = text.splitlines()
    block_re = _block_key_re(keys)
    flow_re = re.compile(rf"^(?:{_alt(keys)}):\s*\S.*$")
    i = None
    single = False
    for n, ln in enumerate(src):
        if block_re.match(ln):
            i = n
            break
        if flow_re.match(ln):
            i = n          # one-line flow form (`chat: {model: x}`)
            single = True
            break
    if i is None:
        if not lines:
            return _joined(src)
        out = src[:]
        if out and out[-1].strip():
            out.append("")
        return "\n".join(out + list(lines)) + "\n"
    if single:
        return _joined(src[:i] + list(lines) + src[i + 1:])
    # consume the block: indented lines, blanks and comments after the
    # key - then give back trailing blank/comment padding, so the spacing
    # that separates the next section stays where it was
    j = i + 1
    while j < len(src) and (not src[j].strip() or src[j].startswith(" ")
                            or src[j].lstrip().startswith("#")):
        j += 1
    k = j
    while k > i + 1 and (not src[k - 1].strip()
                         or src[k - 1].lstrip().startswith("#")):
        k -= 1
    return _joined(src[:i] + list(lines) + src[k:])


def _joined(src) -> str:
    return "\n".join(src) + "\n" if src else ""


# ---------------------------------------------------------------------------
# name-keyed list items (`providers:` and `mcp-servers:`)

def _list_block(src, keys):
    """→ (items=[(start, stop, parsed_dict)], item_indent) or None.
    Item spans exclude trailing blank/comment padding - those lines
    belong to the file (often to the NEXT item), not the edited one."""
    key_re = _block_key_re(keys)
    i = next((n for n, ln in enumerate(src) if key_re.match(ln)), None)
    if i is None:
        return None
    item_re = re.compile(r"^(\s*)- ")
    indent = None
    starts = []
    j = i + 1
    end = i + 1
    while j < len(src):
        ln = src[j]
        if ln.strip() and not ln.startswith(" ") and not ln.startswith("-") \
                and not ln.lstrip().startswith("#"):
            break
        if ln.strip() and (ln.startswith(" ") or ln.startswith("-")):
            end = j + 1
            m = item_re.match(ln)
            if m:
                if indent is None:
                    indent = m.group(1)
                if m.group(1) == indent:
                    starts.append(j)
        j += 1
    items = []
    for n, s0 in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else end
        while stop > s0 + 1 and (not src[stop - 1].strip()
                                 or src[stop - 1].lstrip().startswith("#")):
            stop -= 1
        try:
            got = yaml.safe_load("\n".join(src[s0:stop]))
            parsed = got[0] if isinstance(got, list) and got else None
        except yaml.YAMLError:
            parsed = None
        items.append((s0, stop, parsed if isinstance(parsed, dict) else {}))
    return items, indent or ""


def _find_item(text: str, keys, name: str):
    src = text.splitlines()
    got = _list_block(src, keys)
    if got is None:
        raise ValueError(f"the file has no `{keys[0]}` section")
    items, indent = got
    for s0, stop, parsed in items:
        if str(parsed.get("name") or "") == str(name):
            return src, s0, stop, indent
    raise ValueError(f"no {keys[0]} entry named {name!r} in the file")


def replace_list_item(text: str, keys, name: str, item_lines) -> str:
    """Swap the `- name: …` item called `name` for `item_lines` (unindented
    entry lines, e.g. providers.entry_lines). Everything else - including
    comments between items - stays byte-for-byte."""
    src, s0, stop, indent = _find_item(text, keys, name)
    new = [indent + ln for ln in item_lines]
    return "\n".join(src[:s0] + new + src[stop:]) + "\n"


def remove_list_item(text: str, keys, name: str) -> str:
    """Delete the `- name: …` item called `name`. A section left with no
    items keeps its bare `key:` line (which parses as an empty section)."""
    src, s0, stop, _ = _find_item(text, keys, name)
    return "\n".join(src[:s0] + src[stop:]) + "\n"


# ---------------------------------------------------------------------------
# section serializers - values that match the defaults are OMITTED, so the
# file stays as small as a careful hand would keep it. An all-defaults
# section serializes to [] and replace_section removes it entirely.

_CHAT_PROMPT_DEFAULTS = (
    ("system_prompt", "prompts/system.md"),
    ("compaction_prompt", "prompts/compaction.md"),
    ("title_prompt", "prompts/title.md"),
)


def chat_lines(c: dict) -> list[str]:
    """The `chat:` block for the Chat-defaults dialog's values."""
    c = c if isinstance(c, dict) else {}
    body = []
    if str(c.get("provider") or "").strip():
        body.append(f"  provider: {yq(str(c['provider']).strip())}")
    if str(c.get("model") or "").strip():
        body.append(f"  model: {yq(str(c['model']).strip())}")
    mode = str(c.get("permission_mode") or "").strip()
    if mode and mode != libconfig.DEFAULT_MODE:
        body.append(f"  permission_mode: {yq(mode)}")
    for key, dflt in _CHAT_PROMPT_DEFAULTS:
        v = str(c.get(key) or "").strip()
        if v and v != dflt:
            body.append(f"  {key}: {yq(v)}")
    if c.get("thought_truncation") is False:
        body.append("  thought_truncation: false")
    if c.get("assistant_signals") is False:
        body.append("  assistant_signals: false")
    aname = str(c.get("assistant_name") or "").strip()
    if aname and aname != "loom":
        body.append(f"  assistant_name: {yq(aname)}")
    comp = c.get("compaction") if isinstance(c.get("compaction"), dict) else {}
    sub = []
    if comp.get("auto") is False:
        sub.append("    auto: false")
    thr = comp.get("threshold")
    if thr is not None and abs(float(thr) - 0.8) > 1e-9:
        sub.append(f"    threshold: {float(thr):g}")
    if sub:
        body.append("  compaction:")
        body.extend(sub)
    return ["chat:"] + body if body else []


def permission_mode_lines(modes: dict) -> list[str]:
    """`permission-modes:` from FULL effective {mode: {tool: level}} maps
    (what the permission-modes dialog shows). Only differences from the
    built-ins are written: a builtin mode matching its defaults vanishes
    from the file; a custom mode with no overrides stays as a bare name;
    unknown tools left at "ask" (the runtime default) are dropped."""
    ents = []
    for name, tools in (modes or {}).items():
        name = str(name)
        tools = tools if isinstance(tools, dict) else {}
        base = libconfig.BUILTIN_MODES.get(
            name, libconfig.BUILTIN_MODES[libconfig.DEFAULT_MODE])
        diff = {}
        for t, lv in tools.items():
            lv = str(lv or "").strip().lower()
            if lv not in libconfig.PERM_LEVELS:
                raise ValueError(
                    f"{name}.{t}: the level must be one of "
                    + ", ".join(libconfig.PERM_LEVELS) + f" - not {lv!r}")
            if base.get(str(t), "ask") != lv:
                diff[str(t)] = lv
        if name in libconfig.BUILTIN_MODES and not diff:
            continue
        ents.append((name, diff))
    if not ents:
        return []
    lines = ["permission-modes:"]
    for name, diff in ents:
        lines.append(f"  {yq(name)}:")
        if diff:
            lines.append("    tools:")
            for t in sorted(diff):
                lines.append(f"      {yq(t)}: {diff[t]}")
    return lines


def container_lines(c: dict) -> list[str]:
    """The `containers:` block for the Containers dialog's values."""
    c = c if isinstance(c, dict) else {}
    engine = str(c.get("engine") or "auto").strip().lower()
    body = []
    if engine != "auto":
        body.append(f"  engine: {engine}")
    dflt = str(c.get("default") or "").strip()
    if dflt and dflt != libconfig.DEFAULT_CONTAINERS["default"]:
        body.append(f"  default: {yq(dflt)}")
    rows = []
    for d in c.get("definitions") or []:
        if not isinstance(d, dict) or not str(d.get("name") or "").strip():
            raise ValueError("every container definition needs a name")
        rows.append(f"  - name: {yq(str(d['name']).strip())}")
        if str(d.get("file") or "").strip():
            rows.append(f"    file: {yq(str(d['file']).strip())}")
    if rows:
        body.append("  definitions:")
        body.extend(rows)
    return ["containers:"] + body if body else []
