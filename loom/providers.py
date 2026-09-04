"""Inference providers - the HTTP APIs Loom talks to.

Loom does NOT launch or manage inference processes. A provider is a
running llama.cpp `llama-server` or `ninfer-serve` instance, addressed by
URL - reached directly, or through an ssh stdio tunnel (key auth only)
when the config entry names an `ssh` destination. Models are pulled from
the provider's own API (`GET /v1/models`); nothing about model files or
server flags is Loom's business.

The in-memory registry mirrors what probing found (reachable? which
models? what context window?) and pushes changes to the frontend through
on_status(), the same shape the old server registry used:
    {name, type, url, ssh, state: ok|error|unknown, detail, models, ts}

Context windows:
  * ninfer reports `max_model_len` on each `/v1/models` entry - that IS
    the per-request ceiling.
  * llama-server reports the loaded slot context in `GET /props`
    (`default_generation_settings.n_ctx`) and the model's trained
    context in the `/v1/models` meta (`n_ctx_train`); the served slot
    context wins when present.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error

from loom import sshtunnel

HTTP_TIMEOUT = 10


class ProviderError(Exception):
    pass


# user-facing lines collapse home dirs to ~ (works for remote paths too -
# the pattern, not this process's $HOME, decides)
_TILDE_RE = re.compile(r"(^|[\s='\"(])/(?:home|Users)/[^/\s]+")


def tilde(s: str) -> str:
    return _TILDE_RE.sub(lambda m: m.group(1) + "~", str(s))


def model_key(provider: str, model: str) -> str:
    """The stable id for per-model app state (reasoning prefs, pins)."""
    return f"{provider}::{model}"


# --------------------------------------------------------------------------
# in-memory status registry → the frontend

_lock = threading.Lock()
_providers: dict[str, dict] = {}     # provider name -> status record
_status_cb = None


def on_status(cb) -> None:
    global _status_cb
    _status_cb = cb


def _push_status() -> None:
    cb = _status_cb
    if cb is None:
        return
    try:
        cb({"providers": snapshot()})
    except Exception:
        pass   # the UI feed is best-effort


def snapshot() -> list[dict]:
    with _lock:
        return [dict(v) for v in _providers.values()]


def forget_all() -> None:
    with _lock:
        _providers.clear()
    _push_status()


def forget_name(name: str) -> None:
    with _lock:
        _providers.pop(name, None)
    _push_status()


def status_of(name: str) -> dict:
    with _lock:
        return dict(_providers.get(name) or {})


def models_of(name: str) -> list[dict]:
    """The cached model list for a provider ([{id, ctx}])."""
    with _lock:
        return [dict(m) for m in (_providers.get(name) or {}).get("models") or []]


def model_ctx(name: str, model: str) -> int:
    for m in models_of(name):
        if m.get("id") == model:
            return int(m.get("ctx") or 0)
    return 0


# --------------------------------------------------------------------------
# HTTP

# provider API keys (a llama-server/ninfer started with --api-key) live
# in the OS keyring, never in loom.yaml. The app installs a resolver
# (name -> key); ad-hoc records (the Add dialog's Test) may carry the
# key directly in prov["key"].
_key_resolver = None


def set_key_resolver(fn) -> None:
    global _key_resolver
    _key_resolver = fn


def provider_key(prov: dict) -> str:
    k = str(prov.get("key") or "")
    if k:
        return k
    fn = _key_resolver
    if fn is None:
        return ""
    try:
        return str(fn(str(prov.get("name") or "")) or "")
    except Exception:
        return ""


def request(prov: dict, method: str, path: str, body: bytes | None = None,
            headers: dict | None = None, timeout: float = HTTP_TIMEOUT,
            abort_box: dict | None = None):
    """One HTTP request to a provider (tunneled when it has `ssh`).
    A stored provider key rides as `Authorization: Bearer` - the header
    both llama-server and ninfer accept. Returns the raw response; the
    caller owns close()."""
    url = str(prov.get("url") or "").rstrip("/") + path
    hdrs = dict(headers or {})
    key = provider_key(prov)
    if key:
        hdrs.setdefault("Authorization", f"Bearer {key}")
    return sshtunnel.request(method, url, hdrs or None, body, timeout,
                             str(prov.get("ssh") or ""), abort_box=abort_box)


def _get_json(prov: dict, path: str, timeout: float = HTTP_TIMEOUT) -> dict:
    resp = request(prov, "GET", path, timeout=timeout)
    with resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _where(prov: dict) -> str:
    ssh = str(prov.get("ssh") or "")
    return f"{prov.get('url')} (via ssh {ssh})" if ssh else str(prov.get("url"))


# --------------------------------------------------------------------------
# probing

def probe(prov: dict, register: bool = True) -> dict:
    """Ask one provider what it serves. Updates the registry (unless
    register=False - ad-hoc Test probes must not flash a phantom entry
    through the UI) and returns the status record. Never raises -
    unreachable is a STATE, not an exception (chat sends still raise,
    with fresher detail)."""
    name = str(prov.get("name") or "")
    rec = {"name": name, "type": prov.get("type") or "llama-cpp",
           "url": prov.get("url") or "", "ssh": prov.get("ssh") or "",
           "state": "unknown", "detail": "", "models": [],
           "ts": int(time.time() * 1000)}
    try:
        models = _fetch_models(prov)
        rec["state"] = "ok"
        rec["models"] = models
        rec["detail"] = (f"{len(models)} model(s)" if models
                         else "reachable - no models listed")
    except Exception as e:
        # ANY failure is a state, never a dead probe thread - a provider
        # that answers garbage must show as an error, not as "not probed"
        detail = e
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = f"HTTP {e.code}: " + \
                    e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = f"HTTP {e.code}"
        elif isinstance(e, urllib.error.URLError):
            detail = getattr(e, "reason", e)
        elif not isinstance(e, (ProviderError, OSError, ValueError)):
            detail = f"{type(e).__name__}: {e}"
        rec["state"] = "error"
        rec["detail"] = tilde(f"cannot reach {_where(prov)}: {detail}")
    if register:
        with _lock:
            _providers[name] = rec
        _push_status()
    return dict(rec)


def _fetch_models(prov: dict) -> list[dict]:
    """[{id, ctx}] from the provider's API."""
    got = _get_json(prov, "/v1/models")
    data = got.get("data") if isinstance(got, dict) else None
    if not isinstance(data, list):
        raise ProviderError("the /v1/models answer has no `data` list - "
                            "is this really a llama-server/ninfer API?")
    out = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        entry = {"id": str(m["id"]), "ctx": 0}
        meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
        # ninfer: max_model_len is the per-request context ceiling.
        # llama-server: meta.n_ctx is the usable per-slot context (what -c
        # set, divided by --parallel); n_ctx_train is only the fallback.
        for k in (m.get("max_model_len"), meta.get("n_ctx"),
                  meta.get("n_ctx_train")):
            if k:
                entry["ctx"] = int(k)
                break
        out.append(entry)
    if str(prov.get("type")) == "llama-cpp" and any(not e["ctx"] for e in out):
        # older llama-server builds without meta.n_ctx: /props still
        # carries the served slot context
        try:
            props = _get_json(prov, "/props")
            n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
            if n_ctx:
                for entry in out:
                    entry["ctx"] = entry["ctx"] or int(n_ctx)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError):
            pass   # /props is a nicety; chatting works without it
    return out


def refresh(cfg: dict) -> list[dict]:
    """Probe every configured provider (in parallel - one dead host must
    not delay the others) and converge the registry. Returns the snapshot."""
    provs = (cfg or {}).get("providers") or []
    threads = [threading.Thread(target=probe, args=(p,), daemon=True,
                                name=f"probe-{p.get('name')}")
               for p in provs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=sshtunnel.CONNECT_TIMEOUT_S + HTTP_TIMEOUT + 5)
    # drop registry entries whose provider is gone from the config
    names = {str(p.get("name")) for p in provs}
    with _lock:
        for k in [k for k in _providers if k not in names]:
            del _providers[k]
    _push_status()
    return snapshot()


# --------------------------------------------------------------------------
# loom.yaml `providers:` section - appended non-destructively (the Add
# provider dialog); everything else in the file stays byte-for-byte

def entry_lines(name: str, ptype: str, url: str, ssh: str) -> list[str]:
    def q(s: str) -> str:
        s = str(s)
        if re.search(r"[:#{}\[\],&*?|>'\"%@`!]", s) or s != s.strip():
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return s
    lines = [f"- name: {q(name)}",
             f"  type: {q(ptype)}",
             f"  url: {q(url)}"]
    if ssh:
        lines.append(f"  ssh: {q(ssh)}")
    return lines


def inject_provider(text: str, name: str, ptype: str, url: str,
                    ssh: str = "") -> str:
    """Append one provider entry to the `providers:` block of loom.yaml
    TEXT (creating the block at the end if absent). Handles the shipped
    `providers: []` placeholder (the `[]` is removed) and matches the
    indentation of existing list items."""
    src = text.splitlines()
    # `providers:` optionally followed by an empty flow list and a comment
    key_re = re.compile(r"^providers:\s*(?P<empty>\[\s*\]\s*)?(#.*)?$")
    i = m = None
    for n, ln in enumerate(src):
        m = key_re.match(ln)
        if m:
            i = n
            break
    if i is None:
        out = src[:]
        if out and out[-1].strip():
            out.append("")
        return "\n".join(out + ["providers:"]
                         + entry_lines(name, ptype, url, ssh)) + "\n"
    if m.group("empty"):
        # `providers: []` → open the block; the entry replaces the []
        src[i] = re.sub(r"\[\s*\]\s*", "", src[i]).rstrip()
        return "\n".join(src[:i + 1] + entry_lines(name, ptype, url, ssh)
                         + src[i + 1:]) + "\n"
    # find the end of the block (its last non-comment line) and the
    # indentation its list items actually use
    item_re = re.compile(r"^(\s*)- ")
    indent = ""
    j = i + 1
    end = i + 1
    while j < len(src):
        ln = src[j]
        if ln.strip() and not ln.startswith(" ") and not ln.startswith("-") \
                and not ln.lstrip().startswith("#"):
            break
        if ln.strip() and (ln.startswith(" ") or ln.startswith("-")):
            end = j + 1
            got = item_re.match(ln)
            if got:
                indent = got.group(1)
        j += 1
    new = [indent + ln for ln in entry_lines(name, ptype, url, ssh)]
    return "\n".join(src[:end] + new + src[end:]) + "\n"
