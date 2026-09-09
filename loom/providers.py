"""Inference providers - the HTTP APIs Loom talks to.

Loom does NOT launch or manage inference processes. A provider is
either a llama.cpp `llama-server` you run yourself (a MODERN build -
one that reports `meta.n_ctx` on /v1/models; older builds are not
supported), or a hosted vendor API. Local servers can be reached
through an ssh stdio tunnel (key auth only) when the config entry
names an `ssh` destination.

Vendors: OpenAI, Anthropic, Google Gemini, Google Vertex AI and Amazon
Bedrock. Gemini, Vertex and Bedrock are reached through their
OpenAI-compatible endpoints, so llama-server and every vendor except
Anthropic share one wire dialect; Anthropic's /v1/messages adapter
lives in chat.py. Vendor support is NEW and lightly tested - the UI
says so and asks for feedback.

The in-memory registry mirrors what probing found (reachable? which
models? what context window?) and pushes changes to the frontend
through on_status():
    {name, vendor, url, ssh, state: ok|error|unknown, detail, models, ts}

Context windows: llama-server reports the usable per-slot context as
`meta.n_ctx` on each /v1/models entry (`n_ctx_train` is the fallback).
Hosted vendors don't report one - those models carry ctx 0 and the
context estimator treats the window as unknown.
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


# --------------------------------------------------------------------------
# vendors - the catalog every provider entry names. baseUrl "" means the
# user must supply the URL (their own server / their region+project
# endpoint). chatPath/modelsPath are relative to the entry's url;
# modelsPath None = the vendor has no model listing (type the id).
# extensions = llama.cpp-only request fields (timings_per_token,
# return_progress, cache_prompt).

VENDORS = {
    "llama-cpp": {
        "label": "llama-cpp (llama-server)",
        "baseUrl": "",
        "dialect": "openai",
        "chatPath": "/v1/chat/completions",
        "modelsPath": "/v1/models",
        "extensions": True,
    },
    "openai": {
        "label": "OpenAI",
        "baseUrl": "https://api.openai.com/v1",
        "dialect": "openai",
        "chatPath": "/chat/completions",
        "modelsPath": "/models",
        "extensions": False,
    },
    "anthropic": {
        "label": "Anthropic",
        "baseUrl": "https://api.anthropic.com",
        "dialect": "anthropic",
        "chatPath": "/v1/messages",
        "modelsPath": "/v1/models",
        "extensions": False,
    },
    "gemini": {
        "label": "Google Gemini",
        "baseUrl":
            "https://generativelanguage.googleapis.com/v1beta/openai",
        "dialect": "openai",
        "chatPath": "/chat/completions",
        "modelsPath": "/models",
        "extensions": False,
    },
    "vertex": {
        "label": "Google Vertex AI",
        "baseUrl": "",   # region/project specific - the user supplies it
        "dialect": "openai",
        "chatPath": "/chat/completions",
        "modelsPath": None,
        "extensions": False,
    },
    "bedrock": {
        "label": "Amazon Bedrock",
        "baseUrl":
            "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
        "dialect": "openai",
        "chatPath": "/chat/completions",
        "modelsPath": "/models",
        "extensions": False,
    },
}


def vendor_key(prov: dict) -> str:
    """The provider's vendor id; the legacy `type` key is honored so
    pre-vendor configs and registry records keep working."""
    v = str(prov.get("vendor") or prov.get("type")
            or "llama-cpp").strip().lower()
    return v if v in VENDORS else "llama-cpp"


def vendor_of(prov: dict) -> dict:
    return VENDORS[vendor_key(prov)]


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

# provider API keys (a vendor account key, or a llama-server started
# with --api-key) live in the OS keyring, never in loom.yaml. The app
# installs a resolver (name -> key); ad-hoc records (the Add dialog's
# Test) may carry the key directly in prov["key"].
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
    The stored key rides in the vendor's auth style: `x-api-key` (plus
    the version header) for Anthropic, `Authorization: Bearer`
    everywhere else. Returns the raw response; the caller owns
    close()."""
    url = str(prov.get("url") or "").rstrip("/") + path
    hdrs = dict(headers or {})
    key = provider_key(prov)
    if vendor_key(prov) == "anthropic":
        if key:
            hdrs.setdefault("x-api-key", key)
        hdrs.setdefault("anthropic-version", "2023-06-01")
    elif key:
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
    rec = {"name": name, "vendor": vendor_key(prov),
           "url": prov.get("url") or "", "ssh": prov.get("ssh") or "",
           "state": "unknown", "detail": "", "models": [],
           "ts": int(time.time() * 1000)}
    if vendor_of(prov)["modelsPath"] is None:
        # no listing endpoint to probe (Vertex AI) - usable, on trust
        rec["state"] = "ok"
        rec["detail"] = "this vendor lists no models - type the model id"
        if register:
            with _lock:
                _providers[name] = rec
            _push_status()
        return dict(rec)
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
    """[{id, ctx}] from the provider's model listing."""
    got = _get_json(prov, vendor_of(prov)["modelsPath"])
    data = got.get("data") if isinstance(got, dict) else None
    if not isinstance(data, list):
        raise ProviderError("the models answer has no `data` list - is "
                            "the URL really this vendor's API?")
    out = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        entry = {"id": str(m["id"]), "ctx": 0}
        # llama-server (modern builds): meta.n_ctx is the usable
        # per-slot context (what -c set, divided by --parallel);
        # n_ctx_train is the fallback. max_model_len is the vLLM-style
        # spelling other OpenAI-compatible servers (ninfer included)
        # use for the same ceiling. Hosted vendors report nothing -
        # their ctx stays 0 (unknown).
        meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
        for k in (meta.get("n_ctx"), meta.get("n_ctx_train"),
                  m.get("max_model_len")):
            if k:
                entry["ctx"] = int(k)
                break
        out.append(entry)
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

def entry_lines(name: str, vendor: str, url: str, ssh: str) -> list[str]:
    def q(s: str) -> str:
        s = str(s)
        if re.search(r"[:#{}\[\],&*?|>'\"%@`!]", s) or s != s.strip():
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return s
    lines = [f"- name: {q(name)}",
             f"  vendor: {q(vendor)}",
             f"  url: {q(url)}"]
    if ssh:
        lines.append(f"  ssh: {q(ssh)}")
    return lines


def inject_provider(text: str, name: str, vendor: str, url: str,
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
                         + entry_lines(name, vendor, url, ssh)) + "\n"
    if m.group("empty"):
        # `providers: []` → open the block; the entry replaces the []
        src[i] = re.sub(r"\[\s*\]\s*", "", src[i]).rstrip()
        return "\n".join(src[:i + 1] + entry_lines(name, vendor, url, ssh)
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
    new = [indent + ln for ln in entry_lines(name, vendor, url, ssh)]
    return "\n".join(src[:end] + new + src[end:]) + "\n"
