"""The Models utility — find, download, and distribute GGUF files.

* SCAN a host (localhost automatically; any configured ssh host) for
  .gguf files in the common places: ~/.loom/models, LM Studio's folders,
  the Hugging Face cache, ~/models and ~/Downloads. mmproj files (the
  CLIP projector for a vision model) are flagged and paired with the
  model they sit beside.
* DOWNLOAD a gguf by URL into ~/.loom/models/ (progress events). Every
  download is a persistent RECORD in state.json ("downloads"): it can be
  paused and resumed (HTTP Range on the .part file), and a crash or
  reboot mid-transfer surfaces the record as paused — nothing restarts
  from byte zero.
* PUSH a local model file to a remote host's ~/.loom/models/ over the
  same multiplexed ssh connection the servers use.
* Generate a ready-to-paste `models:` yaml snippet for loom.yaml from a
  selection of scanned files.

Hosts the user configures live in ~/.loom/state.json ("modelHosts").
Events: {type:"models", op, id, kind, ...} with kind progress | done |
error, plus paused | cancelled for downloads.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from loom import ggufmeta, sshtunnel, store


class ModelsError(Exception):
    pass


MODELS_DIR = "~/.loom/models"

# llama.cpp split-GGUF shard suffix (llama-gguf-split writes %05d). The
# FIRST shard is the model: llama-server -m <shard 1> loads the rest from
# the same directory by this exact naming, so shards must never be renamed.
SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}(?=\.gguf$)", re.I)

# searched on every scan; find follows none of these outside $HOME
SCAN_DIRS = (
    "~/.loom/models",
    "~/.lmstudio/models",
    "~/.cache/lm-studio/models",
    "~/.cache/huggingface",
    "~/models",
    "~/Downloads",
)

_SCAN_SCRIPT = (
    "set -u\n"
    + "\n".join(
        f'[ -d {d.replace("~", "$HOME", 1)} ] && '
        f'find {d.replace("~", "$HOME", 1)} -type f -name "*.gguf" '
        '-printf "%s\\t%p\\n" 2>/dev/null'
        for d in SCAN_DIRS)
    + "\ntrue\n")


# ---------------------------------------------------------------------------
# hosts

def hosts() -> list[str]:
    st = store.load_state()
    got = st.get("modelHosts")
    return [str(h) for h in got if str(h).strip()] if isinstance(got, list) else []


def add_host(host: str) -> list[str]:
    h = str(host or "").strip()
    if not h:
        raise ModelsError("give an ssh destination (user@host or a "
                          "~/.ssh/config alias)")
    if h.startswith("-"):
        raise ModelsError("that is an ssh flag, not a host")

    def fn(st):
        cur = st.get("modelHosts")
        cur = cur if isinstance(cur, list) else []
        if h not in cur:
            cur.append(h)
        st["modelHosts"] = cur
    store.mutate_state(fn)
    return hosts()


def reorder_hosts(order: list[str]) -> list[str]:
    """Persist a new host order. `order` must be a permutation of the
    stored hosts — anything else (stale UI, concurrent edit) is refused
    so a drag can never silently drop or invent a host."""
    want = [str(h) for h in order] if isinstance(order, list) else []
    if sorted(want) != sorted(hosts()):
        raise ModelsError("host list changed underneath — reload and retry")

    def fn(st):
        st["modelHosts"] = want
    store.mutate_state(fn)
    return hosts()


def remove_host(host: str) -> list[str]:
    def fn(st):
        cur = st.get("modelHosts")
        st["modelHosts"] = [x for x in cur if x != host] \
            if isinstance(cur, list) else []
    store.mutate_state(fn)
    return hosts()


# ---------------------------------------------------------------------------
# scanning

def _tilde(path: str, home: str) -> str:
    return "~" + path[len(home):] if home and path.startswith(home) else path


def parse_scan(out: str, home: str) -> list[dict]:
    """find output → [{path, size, name, mmproj, dir, pairedWith}].
    llama.cpp split shards (NAME-00001-of-000NN.gguf) in one directory
    collapse into a single entry: path is the FIRST shard (what -m takes),
    size the total, parts every shard path. mmproj files are matched to
    the largest non-mmproj gguf in the same directory (LM Studio and HF
    ship them side by side)."""
    entries = []
    seen = set()
    for line in out.splitlines():
        if "\t" not in line:
            continue
        size_s, path = line.split("\t", 1)
        path = path.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            size = int(size_s)
        except ValueError:
            size = 0
        name = path.rsplit("/", 1)[-1]
        entries.append({
            "path": _tilde(path, home),
            "size": size,
            "name": name,
            "dir": _tilde(path.rsplit("/", 1)[0], home),
            "mmproj": "mmproj" in name.lower(),
            "pairedWith": None,
        })
    entries = _group_shards(entries)
    by_dir: dict[str, list[dict]] = {}
    for e in entries:
        by_dir.setdefault(e["dir"], []).append(e)
    for group in by_dir.values():
        mains = [e for e in group if not e["mmproj"]]
        if not mains:
            continue
        best = max(mains, key=lambda e: e["size"])
        for e in group:
            if e["mmproj"]:
                e["pairedWith"] = best["path"]
    entries.sort(key=lambda e: (e["dir"], e["mmproj"], -e["size"]))
    return entries


def _group_shards(entries: list[dict]) -> list[dict]:
    """Collapse split shards found on disk into one entry per model."""
    groups: dict[tuple, list[dict]] = {}
    out = []
    for e in entries:
        if SPLIT_RE.search(e["name"]):
            groups.setdefault((e["dir"], SPLIT_RE.sub("", e["name"])), []) \
                .append(e)
        else:
            out.append(e)
    for (d, name), shards in groups.items():
        shards.sort(key=lambda s: s["name"])
        out.append({
            "path": shards[0]["path"],
            "size": sum(s["size"] for s in shards),
            "name": name,
            "dir": d,
            "mmproj": "mmproj" in name.lower(),
            "pairedWith": None,
            "parts": [s["path"] for s in shards],
        })
    return out


def scan(host: str = "") -> list[dict]:
    """Find ggufs on `host` ('' = this machine)."""
    try:
        rc, out, err = sshtunnel.run(host, _SCAN_SCRIPT, timeout=60)
    except sshtunnel.RunError as e:
        raise ModelsError(f"cannot reach {host or 'this machine'}: {e}")
    hrc, home_out, _ = 0, "", ""
    try:
        hrc, home_out, _ = sshtunnel.run(host, 'printf "%s" "$HOME"', timeout=20)
    except sshtunnel.RunError:
        pass
    home = home_out.strip() if hrc == 0 else ""
    return parse_scan(out, home)


# ---------------------------------------------------------------------------
# gguf header metadata (trained context for the wizard's slider)

def gguf_meta(path: str, host: str = "") -> dict:
    """{ctx, arch, name, ...} from a GGUF header — locally by reading the
    file, remotely by SHIPPING ggufmeta's source over ssh (it is stdlib-
    only and standalone by design). Best effort: {} on any failure."""
    p = str(path or "").strip()
    if not p:
        return {}
    if not host:
        return ggufmeta.read(str(Path(p).expanduser()))
    try:
        src = Path(ggufmeta.__file__).read_text(encoding="utf-8")
        script = (
            f'p={_shq(p)}\n'
            'case "$p" in "~/"*) p="$HOME${p#\\~}";; esac\n'
            'export LOOM_GGUF_PATH="$p"\n'
            "python3 - <<'LOOM_GGUF_EOF'\n"
            + src +
            "\nimport json as _j, os as _o\n"
            "print('LOOMMETA ' + _j.dumps(read(_o.environ['LOOM_GGUF_PATH'])))\n"
            "LOOM_GGUF_EOF\n")
        rc, out, _err = sshtunnel.run(host, script, timeout=45)
        for line in out.splitlines():
            if line.startswith("LOOMMETA "):
                import json as _json
                got = _json.loads(line[9:])
                return got if isinstance(got, dict) else {}
    except (sshtunnel.RunError, ValueError, OSError):
        pass
    return {}


def _shq(s: str) -> str:
    import shlex
    return shlex.quote(str(s))


# ---------------------------------------------------------------------------
# the yaml snippet

def _display_name(filename: str) -> str:
    base = re.sub(r"\.gguf$", "", SPLIT_RE.sub("", filename), flags=re.I)
    base = re.sub(r"[-_.](Q\d[\w.]*|IQ\d[\w.]*|BF16|F16|F32)$", "", base,
                  flags=re.I)
    return re.sub(r"[-_]+", " ", base).strip() or filename


def _yq(s: str) -> str:
    """Quote a yaml scalar when it needs it."""
    if re.match(r"^[\w~/. -]+$", s) and not s.strip() != s:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# the full everyday flag set — generated entries carry it EXPLICITLY so
# the user sees and owns every flag instead of hunting for hidden defaults
DEFAULT_ENTRY_FLAGS = [
    "-ngl 99",
    "-kvu",
    "-fa on",
    "-ctk q4_0 -ctv q4_0",
    "--spec-type draft-mtp",
    "--spec-draft-n-max 2",
    "--spec-draft-n-min 0",
    "--spec-draft-p-min 0.75",
    "-np 1",
]


def entry_lines(e: dict) -> list[str]:
    """One model entry as loom.yaml lines (no `models:` header).
    e: {path, name?, mmproj?, host?, context?, flags?: [lines], binary?}"""
    path = str(e.get("path") or "").strip()
    if not path:
        raise ModelsError("the entry has no model path")
    name = str(e.get("name") or "").strip() \
        or _display_name(path.rsplit("/", 1)[-1])
    lines = [f"- name: {_yq(name)}"]
    host = str(e.get("host") or "").strip()
    if host:
        lines.append(f"  ssh: {_yq(host)}")
    try:
        ctx = int(e.get("context") or 0)
    except (TypeError, ValueError):
        ctx = 0
    lines.append(f"  context: {ctx or 32768}")
    lines.append(f"  model: {_yq(path)}")
    mm = str(e.get("mmproj") or "").strip()
    if mm:
        lines.append(f"  mmproj: {_yq(mm)}")
    binary = str(e.get("binary") or "").strip()
    if binary and binary != "llama-server":
        lines.append(f"  binary: {_yq(binary)}")
    flags = e.get("flags")
    if isinstance(flags, str):
        flags = [ln for ln in flags.splitlines() if ln.strip()]
    if not isinstance(flags, list) or not flags:
        flags = DEFAULT_ENTRY_FLAGS
    lines.append("  flags: |")
    for fl in flags:
        lines.append(f"    {str(fl).strip()}")
    return lines


def yaml_snippet(selection: list[dict]) -> str:
    """A `models:` block for loom.yaml. Handwritten yaml so the flags land
    as a literal block like the docs show."""
    entries = [e for e in selection if str(e.get("path") or "").strip()]
    if not entries:
        raise ModelsError("select at least one model file")
    lines = ["models:"]
    for e in entries:
        lines += entry_lines(e)
    return "\n".join(lines) + "\n"


_MODELS_KEY_RE = re.compile(r"^models:\s*(\[\s*\])?\s*(#.*)?$")


def inject_model(text: str, entry: dict) -> str:
    """Insert one model entry into loom.yaml TEXT non-destructively."""
    return inject_lines(text, entry_lines(entry))


def snippet_entry_lines(snippet: str) -> list[str]:
    """An edited `models:` snippet → its RAW entry lines (the user's
    formatting and comments preserved), validated to parse as model
    entries first. Accepts a full `models:` block or bare list items."""
    import yaml as _yaml
    from loom import libconfig
    try:
        raw = _yaml.safe_load(str(snippet))
    except _yaml.YAMLError as e:
        raise ModelsError(f"the snippet does not parse as yaml: {e}")
    entries = raw.get("models") if isinstance(raw, dict) else raw
    try:
        parsed = libconfig._models(entries)
    except libconfig.ConfigError as e:
        raise ModelsError(str(e))
    if not parsed:
        raise ModelsError("the snippet contains no model entries")
    lines = str(snippet).splitlines()
    # drop everything up to and including the models: header, if present
    for i, ln in enumerate(lines):
        if _MODELS_KEY_RE.match(ln):
            lines = lines[i + 1:]
            break
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ModelsError("the snippet contains no model entries")
    return lines


def inject_lines(text: str, new: list[str]) -> str:
    """Insert entry LINES into loom.yaml text non-destructively: comments,
    ordering and formatting of everything else are untouched. They land at
    the END of the existing models block (or replace a `models: []`, or a
    new block is appended when the key is absent)."""
    src = text.splitlines()
    key_idx = next((i for i, ln in enumerate(src)
                    if _MODELS_KEY_RE.match(ln)), None)
    if key_idx is None:
        out = src[:]
        if out and out[-1].strip():
            out.append("")
        out += ["models:"] + new
        return "\n".join(out) + "\n"

    m = _MODELS_KEY_RE.match(src[key_idx])
    empty_list = bool(m.group(1))
    comment = (" " + m.group(2)) if m.group(2) else ""
    out = src[:key_idx] + [f"models:{comment}"]
    rest = src[key_idx + 1:]
    if empty_list:
        return "\n".join(out + new + rest) + "\n"
    # walk the models block: list items ('- ' at col 0), indented lines,
    # blanks and comments belong to it; the next top-level key ends it
    j = 0
    while j < len(rest):
        ln = rest[j]
        if ln.strip() == "" or ln.lstrip().startswith("#") \
                or ln.startswith(" ") or ln.startswith("-"):
            j += 1
            continue
        break
    # insert before any trailing blank/comment padding of the block, so
    # the spacing that separates the next section stays where it was
    k = j
    while k > 0 and (rest[k - 1].strip() == ""
                     or rest[k - 1].lstrip().startswith("#")):
        k -= 1
    return "\n".join(out + rest[:k] + new + rest[k:]) + "\n"


# ---------------------------------------------------------------------------
# Hugging Face repo browsing: "owner/name" (or any HF url form) → the
# repo's GGUF files so the user picks a quant

def parse_repo(spec: str) -> str:
    """'unsloth/Qwen-GGUF', 'https://huggingface.co/unsloth/Qwen-GGUF',
    '/tree/main…' and '/resolve/…' url forms → 'owner/name'."""
    s = str(spec or "").strip().rstrip("/")
    s = re.sub(r"^https?://(www\.)?huggingface\.co/", "", s)
    s = re.sub(r"\?.*$", "", s)
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2 and parts[0] not in ("api", "datasets", "spaces"):
        repo = f"{parts[0]}/{parts[1]}"
        if re.match(r"^[\w.-]+/[\w.-]+$", repo):
            return repo
    raise ModelsError(
        f"not a Hugging Face repo: {spec!r} — use owner/name or the repo URL")


def repo_files(spec: str) -> list[dict]:
    """The GGUF files in an HF repo: [{path, size, url}], sorted by name.
    Split shards (one set per quantization) collapse into a single entry —
    see group_split — so the list reads as 'the available quants'."""
    repo = parse_repo(spec)
    api = f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "loom"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json as _json
            tree = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        raise ModelsError(f"could not list {repo}: {e}")
    out = []
    for f in tree if isinstance(tree, list) else []:
        p = str(f.get("path") or "")
        if not p.lower().endswith(".gguf"):
            continue
        out.append({"path": p, "size": int(f.get("size") or 0),
                    "url": f"https://huggingface.co/{repo}/resolve/main/"
                           + urllib.parse.quote(p)})
    out = group_split(out)
    out.sort(key=lambda x: x["path"].lower())
    if not out:
        raise ModelsError(f"{repo} has no .gguf files")
    return out


def group_split(files: list[dict]) -> list[dict]:
    """Collapse split shards into one logical entry per model: path is the
    shard name with the -0000N-of-0000M suffix dropped (display only —
    that file does not exist), url points at the FIRST shard, size is the
    total, and parts lists every shard {path, size, url} in order."""
    groups: dict[str, list[dict]] = {}
    out = []
    for f in files:
        if SPLIT_RE.search(f["path"]):
            groups.setdefault(SPLIT_RE.sub("", f["path"]), []).append(f)
        else:
            out.append(f)
    for path, shards in groups.items():
        shards.sort(key=lambda s: s["path"])
        out.append({"path": path,
                    "size": sum(s["size"] for s in shards),
                    "url": shards[0]["url"],
                    "parts": shards})
    return out


# ---------------------------------------------------------------------------
# download / push — long operations on worker threads, progress via push

_jobs: dict[str, threading.Event] = {}   # push jobs (not resumable)
_jobs_lock = threading.Lock()


def cancel(job_id: str) -> bool:
    with _jobs_lock:
        ev = _jobs.get(str(job_id))
    if ev:
        ev.set()
        return True
    return False


def _job(job_id: str) -> threading.Event:
    ev = threading.Event()
    with _jobs_lock:
        _jobs[str(job_id)] = ev
    return ev


def _job_done(job_id: str) -> None:
    with _jobs_lock:
        _jobs.pop(str(job_id), None)


def local_models_dir() -> Path:
    d = Path(MODELS_DIR).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _emit(push, op: str, job_id: str, kind: str, **kw) -> None:
    try:
        push({"type": "models", "op": op, "id": job_id, "kind": kind, **kw})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# resumable downloads. Each download is a RECORD persisted in state.json
# ("downloads"): {id, urls, names, label, total, done, status, error, ts}
# with status active | paused | error | done. Bytes live in
# ~/.loom/models/ — finished shards under their final names, the shard in
# flight as <name>.part. Resume skips finished shards and continues the
# .part with an HTTP Range request; a server that ignores Range (200
# instead of 206) restarts just that shard. Records outlive the process:
# after a crash or reboot, downloads() surfaces stale "active" records as
# paused, byte counts recomputed from what is actually on disk.

_dl_ctl: dict[str, dict] = {}   # live job id -> {"pause": Event, "cancel": Event}
_dl_lock = threading.Lock()


class _DlCancelled(Exception):
    pass


def _dl_recs(st: dict) -> list[dict]:
    got = st.get("downloads")
    return [r for r in got if isinstance(r, dict)] \
        if isinstance(got, list) else []


def _dl_get(job_id: str) -> dict | None:
    for r in _dl_recs(store.load_state()):
        if r.get("id") == job_id:
            return dict(r)
    return None


def _dl_update(job_id: str, **fields) -> None:
    def fn(st):
        recs = _dl_recs(st)
        for r in recs:
            if r.get("id") == job_id:
                r.update(fields)
        st["downloads"] = recs
    store.mutate_state(fn)


def _dl_remove(job_id: str) -> None:
    def fn(st):
        st["downloads"] = [r for r in _dl_recs(st) if r.get("id") != job_id]
    store.mutate_state(fn)


def _dl_disk_done(rec: dict) -> int:
    """Bytes of this record already on disk (finished shards + .part)."""
    ddir = local_models_dir()
    got = 0
    for n in rec.get("names") or []:
        p = ddir / str(n)
        t = p.with_name(p.name + ".part")
        if p.is_file():
            got += p.stat().st_size
        elif t.is_file():
            got += t.stat().st_size
    return got


def downloads() -> list[dict]:
    """The download registry for the UI. Records a crash left 'active'
    (no live worker) surface as paused, and non-active byte counts are
    recomputed from disk — the record's counter may be stale."""
    with _dl_lock:
        live = set(_dl_ctl)
    out = []
    for r in _dl_recs(store.load_state()):
        r = dict(r)
        if r.get("status") == "active" and r.get("id") not in live:
            r["status"] = "paused"
            _dl_update(str(r.get("id")), status="paused")
        if r.get("status") in ("paused", "error"):
            r["done"] = _dl_disk_done(r)
        out.append(r)
    return out


def start_download(push, job_id: str, url, filename: str = "",
                   total: int = 0) -> None:
    """Begin a new download record and its worker thread. `url` may be a
    LIST of shard urls (a split model) — every shard keeps its exact name
    (llama.cpp finds siblings by it). `total` is the expected byte total
    when the caller knows it (the repo listing does)."""
    urls = [str(u or "").strip()
            for u in (url if isinstance(url, (list, tuple)) else [url])]
    urls = [u for u in urls if u]
    if not urls or not all(u.startswith(("http://", "https://"))
                           for u in urls):
        raise ModelsError("give an http(s) URL to a .gguf file")
    names = []
    for u in urls:
        n = (str(filename or "").strip() if len(urls) == 1 else "") or \
            urllib.parse.unquote(u.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
        n = re.sub(r"[^\w.+-]", "_", n)
        if not n.lower().endswith(".gguf"):
            n += ".gguf"
        names.append(n)
    ddir = local_models_dir()
    for n in names:
        if (ddir / n).exists():
            raise ModelsError(f"{n} already exists in ~/.loom/models")
    taken = {n for r in _dl_recs(store.load_state())
             if r.get("status") != "done" for n in (r.get("names") or [])}
    for n in names:
        if n in taken:
            raise ModelsError(
                f"{n} is already being downloaded — resume or cancel it "
                "in the Downloader")
    try:
        grand = max(0, int(total or 0))
    except (TypeError, ValueError):
        grand = 0
    rec = {"id": str(job_id), "urls": urls, "names": names,
           "label": SPLIT_RE.sub("", names[0]), "total": grand, "done": 0,
           "status": "active", "error": "", "ts": int(time.time() * 1000)}

    def fn(st):
        st["downloads"] = _dl_recs(st) + [rec]
    store.mutate_state(fn)
    _dl_spawn(push, rec)


def resume_download(push, job_id: str) -> None:
    """Continue a paused/errored/interrupted download from its .part."""
    rec = _dl_get(str(job_id))
    if rec is None:
        raise ModelsError("no such download")
    with _dl_lock:
        if str(job_id) in _dl_ctl:
            return   # already running
    if rec.get("status") == "done":
        raise ModelsError("that download is already complete")
    _dl_update(str(job_id), status="active", error="")
    rec["status"] = "active"
    _dl_spawn(push, rec)


def pause_download(job_id: str) -> bool:
    with _dl_lock:
        ctl = _dl_ctl.get(str(job_id))
    if ctl:
        ctl["pause"].set()
        return True
    return False


def cancel_download(job_id: str) -> None:
    """Abort a download and delete everything it wrote (record included).
    Running worker → it cleans up on its way out; idle record → clean up
    here."""
    jid = str(job_id)
    with _dl_lock:
        ctl = _dl_ctl.get(jid)
    if ctl:
        ctl["cancel"].set()
        return
    rec = _dl_get(jid)
    if rec is not None:
        _dl_wipe(rec)
        _dl_remove(jid)


def dismiss_download(job_id: str) -> None:
    """Drop a finished record from the registry — the files stay."""
    _dl_remove(str(job_id))


def clear_downloads() -> None:
    """Clear the download HISTORY: every done record goes, files stay.
    Active, paused and errored records are live state, not history —
    they survive (cancel is the way to drop those)."""
    def fn(st):
        st["downloads"] = [r for r in _dl_recs(st)
                           if r.get("status") != "done"]
    store.mutate_state(fn)


def _dl_wipe(rec: dict) -> None:
    ddir = local_models_dir()
    for n in rec.get("names") or []:
        p = ddir / str(n)
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".part").unlink(missing_ok=True)


def _dl_spawn(push, rec: dict) -> None:
    ctl = {"pause": threading.Event(), "cancel": threading.Event()}
    with _dl_lock:
        _dl_ctl[rec["id"]] = ctl
    threading.Thread(target=_dl_run, args=(push, rec, ctl), daemon=True,
                     name=f"dl-{rec['id']}").start()


def _dl_run(push, rec: dict, ctl: dict) -> None:
    job_id = rec["id"]
    label = rec.get("label") or ""
    grand = int(rec.get("total") or 0)
    ddir = local_models_dir()
    got = 0
    try:
        est = 0
        last = 0.0
        last_save = 0.0
        for u, n in zip(rec["urls"], rec["names"]):
            dest = ddir / str(n)
            if dest.is_file():                    # finished on an earlier run
                got += dest.stat().st_size
                continue
            tmp = dest.with_name(dest.name + ".part")
            offset = tmp.stat().st_size if tmp.is_file() else 0
            headers = {"User-Agent": "loom"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(u, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                if offset and getattr(resp, "status", 200) != 206:
                    offset = 0                    # Range ignored — redo shard
                est += offset + int(resp.headers.get("Content-Length") or 0)
                got += offset
                with open(tmp, "ab" if offset else "wb") as f:
                    while True:
                        if ctl["cancel"].is_set():
                            raise _DlCancelled()
                        if ctl["pause"].is_set():
                            _dl_update(job_id, status="paused", done=got)
                            _emit(push, "download", job_id, "paused",
                                  name=label, done=got, total=grand or est)
                            return
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        now = time.monotonic()
                        if now - last > 0.5:
                            last = now
                            _emit(push, "download", job_id, "progress",
                                  name=label, done=got, total=grand or est)
                        if now - last_save > 10:
                            last_save = now
                            _dl_update(job_id, done=got)
            tmp.replace(dest)
        _dl_update(job_id, status="done", done=got,
                   doneTs=int(time.time() * 1000))
        _emit(push, "download", job_id, "done", name=label,
              path=str(ddir / rec["names"][0]), size=got,
              parts=len(rec["names"]))
    except _DlCancelled:
        _dl_wipe(rec)
        _dl_remove(job_id)
        _emit(push, "download", job_id, "cancelled", name=label)
    except Exception as e:
        # bytes stay on disk — the record is resumable
        _dl_update(job_id, status="error", error=str(e), done=got)
        _emit(push, "download", job_id, "error", name=label, msg=str(e),
              done=got, total=grand)
    finally:
        with _dl_lock:
            if _dl_ctl.get(job_id) is ctl:
                del _dl_ctl[job_id]


def start_push(push, job_id: str, local_path, host: str) -> None:
    """Stream a local model file — or a LIST of split shards — to
    `host`:~/.loom/models/ over the multiplexed ssh connection (each file
    atomic: .part then mv)."""
    paths = local_path if isinstance(local_path, (list, tuple)) \
        else [local_path]
    srcs = [Path(str(p)).expanduser() for p in paths]
    for i, src in enumerate(srcs):
        if not src.is_file():
            raise ModelsError(f"no such file: {paths[i]}")
    if not srcs:
        raise ModelsError("no file to push")
    h = str(host or "").strip()
    if not h or h.startswith("-"):
        raise ModelsError("pick a configured ssh host")
    names = [re.sub(r"[^\w.+-]", "_", src.name) for src in srcs]
    label = SPLIT_RE.sub("", names[0])
    total = sum(src.stat().st_size for src in srcs)
    cancel_ev = _job(job_id)

    def work():
        try:
            rc, _o, err = sshtunnel.run(
                h, 'mkdir -p "$HOME/.loom/models"', timeout=30)
            if rc != 0:
                raise ModelsError(f"mkdir on {h} failed: {err.strip()[:200]}")
            sent = 0
            last = 0.0
            import time as _t
            for src, name in zip(srcs, names):
                remote = f'$HOME/.loom/models/{name}'
                argv = [*sshtunnel.SSH_CMD, *sshtunnel._mux_args(), h,
                        f'cat > "{remote}.part" && '
                        f'mv "{remote}.part" "{remote}"']
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        env=sshtunnel._ssh_env(),
                                        start_new_session=True)
                with open(src, "rb") as f:
                    while True:
                        if cancel_ev.is_set():
                            proc.kill()
                            raise ModelsError("cancelled")
                        chunk = f.read(1024 * 512)
                        if not chunk:
                            break
                        proc.stdin.write(chunk)
                        sent += len(chunk)
                        now = _t.monotonic()
                        if now - last > 0.5:
                            last = now
                            _emit(push, "push", job_id, "progress",
                                  name=label, host=h, done=sent, total=total)
                proc.stdin.close()
                rc = proc.wait(timeout=120)
                if rc != 0:
                    err = proc.stderr.read().decode("utf-8", "replace")[:300]
                    raise ModelsError(f"transfer failed (rc {rc}): {err}")
            _emit(push, "push", job_id, "done", name=label, host=h,
                  path=f"~/.loom/models/{names[0]}")
        except Exception as e:
            _emit(push, "push", job_id, "error", name=label, host=h,
                  msg=str(e))
        finally:
            _job_done(job_id)
    threading.Thread(target=work, daemon=True, name=f"push-{job_id}").start()
