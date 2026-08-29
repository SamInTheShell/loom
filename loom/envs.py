"""Named environments — sets of environment variables a chat can load
into its shell containers (cloud keys, API tokens, plain settings).

Split storage, deliberately:

  * `environments.yaml` at the LIBRARY root holds the definitions —
    names, plain (non-secret) values, and SECRET STUBS: a key mapped to
    null. Stubs are indicators — anyone opening the library sees which
    variables a container needs, which is exactly what troubleshooting
    requires. The file is shareable and commit-safe by construction.
  * Secret VALUES never touch the library. They live in the OS keyring
    (Secret Service / KWallet / Keychain via `keyring`), one JSON blob
    per environment name, set through the Environments tab per machine.

At shell time the two merge: plain values from the file, secret values
from the keyring. A stub with no local keyring value stays UNSET in the
container — the honest failure mode — and is reported as missing so
the model (and the user) can say what to fix.
"""

from __future__ import annotations

import json
import re

import yaml

from loom import library

SERVICE = "loom-environments"
FILENAME = "environments.yaml"

_HEADER = """\
# Environments — sets of variables for chat shell containers.
# Pick one per chat next to the permission-mode pill.
#
# Plain values live right here and travel with the library. A key with
# NO value (null) is a SECRET STUB: the real value lives in your OS
# keyring, set via the Environments tab on each machine — the stub
# documents that the variable is required without ever storing it.
#
# aws-dev:
#   AWS_REGION: us-east-1        # plain — shared with the library
#   AWS_ACCESS_KEY_ID:           # secret — value in the keyring
#   AWS_SECRET_ACCESS_KEY:       # secret
"""

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvError(Exception):
    pass


# keyring access behind thin wrappers so tests can swap in an in-memory
# backend (CI has no Secret Service)
def _kr_get(name: str):
    import keyring
    return keyring.get_password(SERVICE, name)


def _kr_set(name: str, blob: str) -> None:
    import keyring
    keyring.set_password(SERVICE, name, blob)


def _kr_del(name: str) -> None:
    import keyring
    import keyring.errors
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def _check_name(name: str) -> str:
    n = str(name or "").strip()
    if not _NAME_RE.match(n):
        raise EnvError("environment names: letters, digits, spaces, "
                       "._- (max 64 chars)")
    return n


def _check_key(key: str) -> str:
    k = str(key or "").strip()
    if not _KEY_RE.match(k):
        raise EnvError(f"bad variable name {k!r} — letters, digits, "
                       "underscores, not starting with a digit")
    return k


# ---------------------------------------------------------------------------
# the library file: {env name: {KEY: plain value | None (= secret stub)}}

def read_defs(root) -> dict:
    p = library.safe_join(root, FILENAME)
    if not p.is_file():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise EnvError(f"{FILENAME}: {e}")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EnvError(f"{FILENAME} must map environment names to variables")
    out: dict = {}
    for name, body in raw.items():
        n = _check_name(name)
        if body is None:
            out[n] = {}
            continue
        if not isinstance(body, dict):
            raise EnvError(f"{FILENAME}: {n} must be a mapping of variables")
        out[n] = {_check_key(k): (None if v is None else str(v))
                  for k, v in body.items()}
    return out


def write_defs(root, defs: dict) -> None:
    """Loom owns this file's shape: header comment + plain yaml dump."""
    p = library.safe_join(root, FILENAME)
    body = yaml.safe_dump(defs, sort_keys=True, default_flow_style=False,
                          allow_unicode=True) if defs else ""
    p.write_text(_HEADER + "\n" + body, encoding="utf-8")


def list_envs(root) -> list[str]:
    return sorted(read_defs(root))


# ---------------------------------------------------------------------------
# secrets (per machine, keyed by environment name)

def _secrets(name: str) -> dict:
    try:
        raw = _kr_get(name)
    except Exception as e:
        raise EnvError(f"no usable system keyring: {e}")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}


def set_secrets(name: str, values: dict) -> None:
    """Merge secret values for an environment into the keyring. An empty
    string value REMOVES that secret."""
    n = _check_name(name)
    cur = _secrets(n)
    for k, v in (values or {}).items():
        key = _check_key(k)
        val = str(v if v is not None else "")
        if "\n" in val or "\r" in val:
            raise EnvError(f"{key}: values cannot contain newlines")
        if val:
            cur[key] = val
        else:
            cur.pop(key, None)
    try:
        if cur:
            _kr_set(n, json.dumps(cur))
        else:
            _kr_del(n)
    except EnvError:
        raise
    except Exception as e:
        raise EnvError(f"no usable system keyring: {e}")


def secret_status(root, name: str) -> dict:
    """{KEY: bool} — which of an environment's secret stubs have a value
    in THIS machine's keyring."""
    n = _check_name(name)
    defs = read_defs(root)
    if n not in defs:
        raise EnvError(f"no environment named {n!r}")
    have = _secrets(n)
    return {k: (k in have and bool(have[k]))
            for k, v in defs[n].items() if v is None}


def save_env(root, name: str, variables: list, secret_values: dict) -> list[str]:
    """Create/replace one environment. variables: [{key, value, secret}]
    — secret rows land as stubs in the file; their non-empty values (and
    removals via empty string in secret_values) go to the keyring."""
    n = _check_name(name)
    body: dict = {}
    for row in variables or []:
        if not isinstance(row, dict):
            continue
        key = _check_key(row.get("key"))
        if row.get("secret"):
            body[key] = None
        else:
            val = str(row.get("value") if row.get("value") is not None else "")
            if "\n" in val or "\r" in val:
                raise EnvError(f"{key}: values cannot contain newlines")
            body[key] = val
    defs = read_defs(root)
    defs[n] = body
    write_defs(root, defs)
    if secret_values:
        set_secrets(n, {k: v for k, v in secret_values.items()
                        if _check_key(k) in body and body[_check_key(k)] is None})
    # secrets whose stub was deleted stop mattering; drop them from the
    # keyring so nothing lingers unreferenced
    stale = {k: "" for k in _secrets(n) if k not in body}
    if stale:
        set_secrets(n, stale)
    return list_envs(root)


def delete_env(root, name: str) -> list[str]:
    n = _check_name(name)
    defs = read_defs(root)
    defs.pop(n, None)
    write_defs(root, defs)
    try:
        _kr_del(n)
    except Exception:
        pass
    return list_envs(root)


# ---------------------------------------------------------------------------
# resolution (shell time)

def resolve(root, name: str) -> tuple[dict, list[str]]:
    """(variables to inject, secret keys MISSING on this machine)."""
    n = _check_name(name)
    defs = read_defs(root)
    if n not in defs:
        raise EnvError(f"no environment named {n!r}")
    have = _secrets(n)
    out: dict = {}
    missing: list[str] = []
    for k, v in defs[n].items():
        if v is None:
            if have.get(k):
                out[k] = have[k]
            else:
                missing.append(k)
        else:
            out[k] = v
    return out, sorted(missing)
