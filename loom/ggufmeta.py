"""GGUF header metadata — context length, architecture, name.

Deliberately STANDALONE: stdlib only, no codetree imports, no package
relative imports. modelfiles imports it for local files AND ships this very
file's source to an SSH host to read the headers there, so a remote model
list carries the same facts as a local one instead of a shrug. One
implementation, two places to run it.

Just enough of the GGUF spec to walk the kv header: magic, version, tensor
count, then key/value pairs. Tokenizer arrays are huge, so values are
SKIPPED by size and never materialized.

As a script: reads file paths on stdin (one per line), prints one JSON
object per line for the files it could read anything from.
"""

from __future__ import annotations

import struct

MAGIC = b"GGUF"
_T_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_T_STR, _T_ARR = 8, 9


def _read_str(f) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    if n > 1 << 20:
        raise ValueError("implausible string length")
    return f.read(n).decode("utf-8", "replace")


def _skip_value(f, typ: int) -> None:
    if typ in _T_SIZES:
        f.seek(_T_SIZES[typ], 1)
    elif typ == _T_STR:
        (n,) = struct.unpack("<Q", f.read(8))
        f.seek(n, 1)
    elif typ == _T_ARR:
        (ityp,) = struct.unpack("<I", f.read(4))
        (cnt,) = struct.unpack("<Q", f.read(8))
        if ityp in _T_SIZES:
            f.seek(_T_SIZES[ityp] * cnt, 1)
        elif ityp == _T_STR:
            for _ in range(cnt):
                (n,) = struct.unpack("<Q", f.read(8))
                f.seek(n, 1)
        else:
            raise ValueError("nested arrays")
    else:
        raise ValueError(f"unknown kv type {typ}")


def _read_scalar(f, typ: int):
    fmt = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
           6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d"}.get(typ)
    if fmt is None:
        return None
    (v,) = struct.unpack(fmt, f.read(struct.calcsize(fmt)))
    return v


# The shape fields a KV-cache estimate needs. All are "<arch>.<suffix>",
# so they are matched by suffix rather than by knowing every architecture.
_SHAPE_SUFFIX = {
    ".block_count": "layers",
    ".attention.head_count": "heads",
    ".attention.head_count_kv": "kvHeads",
    ".attention.key_length": "keyLen",
    ".attention.value_length": "valLen",
    ".embedding_length": "embedding",
}


def read(path: str) -> dict:
    """What one GGUF header says about itself: {ctx, arch, name} plus the
    shape fields a memory estimate needs (layers, heads, kvHeads, keyLen,
    valLen, embedding). Never raises — a truncated or odd file yields {}.

    The whole kv section is walked now rather than stopping early: the
    shape keys are scattered through it, and skipping values is cheap
    (arrays are seeked over, never read)."""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != MAGIC:
                return {}
            (version,) = struct.unpack("<I", f.read(4))
            if version < 2:
                return {}
            f.seek(8, 1)                       # tensor count
            (n_kv,) = struct.unpack("<Q", f.read(8))
            if n_kv > 4096:
                return {}
            for _ in range(n_kv):
                key = _read_str(f)
                (typ,) = struct.unpack("<I", f.read(4))
                field = next((v for suf, v in _SHAPE_SUFFIX.items() if key.endswith(suf)), None)
                if key.endswith(".context_length"):
                    out["ctx"] = int(_read_scalar(f, typ) or 0) or None
                elif key == "general.architecture" and typ == _T_STR:
                    out["arch"] = _read_str(f)
                elif key == "general.name" and typ == _T_STR:
                    out["name"] = _read_str(f)
                elif field and typ != _T_STR and typ != _T_ARR:
                    v = _read_scalar(f, typ)
                    if v:
                        out[field] = int(v)
                else:
                    _skip_value(f, typ)
    except (OSError, ValueError, struct.error):
        pass
    return out


def _main() -> None:
    import json
    import sys

    for line in sys.stdin:
        path = line.rstrip("\n")
        if not path:
            continue
        meta = read(path)
        if meta:
            meta["p"] = path
            sys.stdout.write(json.dumps(meta) + "\n")


if __name__ == "__main__":
    _main()
