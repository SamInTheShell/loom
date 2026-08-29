"""Fuzzy find-in-files for the Library tab (and the knowledge_search tool).

Two result kinds from one query, ranked together:
  * file paths, scored by fuzzy SUBSEQUENCE match (a la Ctrl+P)
  * content lines, case-insensitive substring match, with line numbers

Everything is computed live on request — a library is small (markdown,
yaml, container files); an index would be more machinery than the data
deserves.
"""

from __future__ import annotations

from pathlib import Path

from loom import library

MAX_FILE_BYTES = 1_000_000       # content search skips bigger files
MAX_RESULTS = 60


def _fuzzy_score(query: str, target: str) -> float:
    """Subsequence match score: 0 = no match. Rewards adjacency, starts of
    words, and shorter targets."""
    q = query.lower()
    t = target.lower()
    if not q:
        return 0.0
    score, ti, streak = 0.0, 0, 0
    for ch in q:
        found = t.find(ch, ti)
        if found < 0:
            return 0.0
        if found == ti:
            streak += 1
            score += 2 + streak          # adjacent run — best signal
        else:
            streak = 0
            score += 1
        if found == 0 or t[found - 1] in "/_-. ":
            score += 3                   # word/segment start
        ti = found + 1
    return score / (1 + len(t) / 40)     # mild short-target preference


def _iter_files(root: Path, subdir: str = ""):
    base = library.safe_join(root, subdir) if subdir else root
    if not base.is_dir():
        return
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                # internals holds chat transcripts — never search fodder
                if p.name not in library.SKIP_DIRS \
                        and p.name != library.INTERNALS:
                    stack.append(p)
            elif p.is_file():
                yield p


def search(root: Path, query: str, subdir: str = "",
           limit: int = MAX_RESULTS) -> list[dict]:
    """Ranked results: [{kind: 'file'|'line', rel, score, line?, text?}]."""
    q = str(query or "").strip()
    if not q:
        return []
    results: list[dict] = []
    ql = q.lower()
    for p in _iter_files(root, subdir):
        rel = str(p.relative_to(root))
        fs = _fuzzy_score(q, rel)
        if fs > 0:
            results.append({"kind": "file", "rel": rel, "score": fs + 5})
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if library.looks_binary(data):
            continue
        text = data.decode("utf-8", "replace")
        hits = 0
        for n, line in enumerate(text.splitlines(), 1):
            if ql in line.lower():
                snippet = line.strip()
                if len(snippet) > 200:
                    at = snippet.lower().find(ql)
                    snippet = "…" + snippet[max(0, at - 60):at + 140] + "…"
                results.append({"kind": "line", "rel": rel, "line": n,
                                "text": snippet,
                                "score": 4 - min(3, hits)})
                hits += 1
                if hits >= 8:            # one file must not flood the list
                    break
    results.sort(key=lambda r: -r["score"])
    return results[:max(1, min(int(limit or MAX_RESULTS), 200))]
