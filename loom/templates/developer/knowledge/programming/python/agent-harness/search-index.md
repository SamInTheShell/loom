# Search index — hybrid transcript + code search in one SQLite file

A workbench accumulates transcripts and repositories; the user needs to
find "where did the agent say X" and "which file defines Y" without a
search server. The whole answer fits in ONE SQLite database
(`~/.yourapp/index.db`): a `chunks` table, an FTS5 mirror of it, and an
embedding-vector table — three layers with very different costs, each
under an explicit user toggle. Full-text is always on and free (no
model involved). Vectors are opt-in: the user picks an embedding
provider + model (llm-providers.md), chunks embed in a background
thread, queries cosine-rank them. Code is indexed only for repo·branch
targets the user adds explicitly. Never index silently and never guess
what the user wants indexed — per-category toggles plus explicit
rebuild buttons with real counts are the contract.

## Schema — one db, three tables

```sql
CREATE TABLE chunks(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- message|response|tool_call|
                                 -- tool_result|thought|code
    chat_id TEXT, msg_idx INTEGER,
    repo_id TEXT, branch TEXT, path TEXT,
    line_start INTEGER, line_end INTEGER,
    ts INTEGER,
    sha TEXT NOT NULL,           -- embedding cache key (below)
    text TEXT NOT NULL);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='chunks', content_rowid='id',
    tokenize='unicode61');
CREATE TABLE vectors(sha TEXT PRIMARY KEY,
                     dim INTEGER NOT NULL, emb BLOB NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
```

`chunks_fts` is an external-content table kept in sync by AFTER
INSERT / AFTER DELETE triggers (the delete trigger uses the special
`INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete',…)`
form). Index `chat_id`, `kind`, `(repo_id, branch)` and `sha`. Open
the connection once with `check_same_thread=False`, guard every use
with one `threading.RLock`, and set `PRAGMA journal_mode=WAL` +
`synchronous=NORMAL` — the embedder thread and the UI thread share it.

## Chunking transcripts

One row per message / tool call / tool result / thought. Constants:
split texts longer than 2000 chars into 2000-char pieces with 200
overlap. Per message role:

- user → kind `message`; each text/doc attachment's inline text
  (attachments-extraction.md) is an extra `message` row prefixed
  `[attachment <name>]`.
- assistant → inline `<think>…</think>` segments (regex, tolerate a
  missing close tag at stream end) become `thought` rows; the
  remainder with think-blocks blanked is the `response` row.
- tool → one `tool_call` row (tool name + args + display params
  joined with spaces) and one `tool_result` row (summary + detail).

Default kind toggles: everything on except `thought` — reasoning is
noisy and users opt into searching it. Re-indexing a chat is
delete-and-replace: `DELETE FROM chunks WHERE chat_id=?` then insert
fresh rows. No diffing — a transcript is small, and the sha keying
below makes redone chunks free on the vector side.

## Incremental updates, debounced

The transcript store calls a hook on every save; tool storms save per
result, so coalesce: put the (chat_id → messages) pair in a pending
dict and start a 2-second `threading.Timer` if none is running; the
flush re-indexes each pending chat once. Never index on the UI thread
and never per-keystroke.

Per-chat opt-out lives in the index's OWN `meta` table
(`noindex:<chat_id>` key), not in workspace config — the hot save path
must not parse a JSON config per write. Disabling drops the chat's
chunks immediately; enabling re-indexes from the persisted transcript
(persistence.md); deleting the chat deletes chunks AND the flag.

## Embedding cache keying — the load-bearing trick

Every chunk row stores `sha = sha256(sig + "\x00" + text)` where
`sig = f"{provider}:{model}"` is the identity of the embedding space.
The `vectors` table is keyed by that sha alone. Consequences, all
deliberate:

- Switching models changes `sig`, so every lookup MISSES — you can
  never mix vectors from incompatible spaces. Old vectors become dead
  weight until an explicit "rebuild vectors" drops the table.
- Re-running any index job skips work already embedded: pending =
  `chunks LEFT JOIN vectors ON v.sha=c.sha WHERE v.sha IS NULL`,
  grouped by sha.
- Re-chunking a chat orphans nothing: identical text → identical sha
  → the existing vector still matches.

The background embedder is one daemon thread woken by an Event (300 s
fallback timeout): drain pending shas in batches of 32, one provider
round-trip per batch (120 s timeout, each input clipped to 8000
chars), `INSERT OR REPLACE` results. On provider error, surface the
message once via the event bus and STOP until the next wake (settings
change, new content) — never hammer a broken endpoint in a loop.
Endpoint shapes are in llm-providers.md; the ones that matter:
OpenAI-compatible `POST {base}/embeddings` with
`{"model":…, "input":[…]}` (sort the response by `index`), Ollama is
the same after appending `/v1`, Gemini uses `batchEmbedContents`.
Anthropic has NO embeddings API — refuse those providers with a clear
error, don't fall through to a 404. Provide a "test embedding" button
that does one honest roundtrip and reports dimension + latency.

## Vector storage and scoring — float16, L2 at write, blockwise

Normalize at write time so cosine similarity becomes a dot product,
then store half-precision (half the bytes, ranking is insensitive to
fp16 rounding):

```python
a = np.asarray(vec, dtype=np.float32)
n = np.linalg.norm(a)
if n > 0: a /= n
blob = a.astype(np.float16).tobytes()   # store (a.size, blob)
```

At query time embed the query (one provider call), normalize it the
same way, load the scope's vectors as one fp16 matrix, and score
blockwise so peak memory stays bounded on large corpora:

```python
mat = np.frombuffer(b"".join(blobs), dtype=np.float16) \
        .reshape(nrows, dim)
scores = np.empty(nrows, dtype=np.float32)
B = 8192                            # rows upcast per block
for i in range(0, nrows, B):
    scores[i:i+B] = mat[i:i+B].astype(np.float32) @ qvec
k = min(limit, nrows)
top = np.argpartition(scores, -k)[-k:]
top = top[np.argsort(scores[top])[::-1]]
```

Cache `(ids, matrix)` per scope (sorted kinds + repo + branch), at
most 3 scopes and only while ≤ 200 000 rows, invalidated by a
generation counter bumped on every vector write or wipe. Two honest
degradations: rows whose stored `dim` differs from the first row are
dropped from the matrix (a mid-switch index scores what matches), and
a query/matrix dimension mismatch raises "rebuild vectors", never a
numpy shape traceback.

## Full-text queries

User text goes into FTS5 MATCH only after sanitizing — quote every
whitespace-split term (double internal quotes) so `NEAR`/`OR`/`-`
cannot be injected, and suffix the LAST term with `*` for
as-you-type prefix feel. Rank with `bm25(chunks_fts)` ascending, pull
display text with `snippet(chunks_fts, 0, '【', '】', ' … ', 14)` —
distinctive markers the UI strips or highlights. Wrap execution in a
try/except for `sqlite3.OperationalError` and return `[]`; a garbled
query is not an error state.

Lexical and vector results are NOT merged into one ranked list. The
public `search(q, modes, kinds, limit, repo_id, branch)` returns
`{"text": [...], "semantic": [...]}` per requested mode; BM25 ranks
and cosine scores are incommensurable, and the UI presents the lists
side by side (fuzzy title matching is label-level and lives entirely
in the frontend). Semantic search with vectors disabled raises a
clear "enable embeddings in Settings" error — an empty list would
read as "no matches", which is a lie.

## Result shape — jump links

Every hit carries enough to navigate without another query: chunk id,
kind, `chat_id` + `msg_idx` for transcript hits, `repo_id` + `branch`
+ `path` + `line_start`/`line_end` for code hits, a snippet, and the
score. The frontend turns these into hrefs (`#/chat/<id>`,
`#/repo/<id>/tree/<branch>/<path>`) — keep routing knowledge out of
the index module.

## Code indexing — explicit repo·branch targets

Targets are a user-managed list `[{repoId, branch}]`. Indexing one
target lists blobs at the branch head (`git ls-tree -r -l`), skips
symlinks, files over 400 KB (generated/vendored) and anything with a
NUL byte in the first 8 KB, then windows each file into 120-line
chunks with 20-line overlap, each capped at 6000 chars, `kind='code'`
with path + line range for jump links. Rebuild is one
`DELETE … WHERE repo_id=? AND branch=?` plus re-insert. Branch heads
move constantly — never auto-reindex on commit; the rebuild button is
the refresh.

## Rebuild UX — honesty as a feature

Rebuilds are explicit buttons, one job at a time (a second request
errors with the running job's name), progress pushed as events
(done/total per chat or per target). Back the buttons with real
numbers: per-kind chunk counts, vector count, chats covered, chunks
per code target, pending embeds — all cheap GROUP BY queries.
"Rebuild vectors" deletes the `vectors` table only (chunks and FTS
untouched); the embedder regenerates from the current model in the
background. Say "time depends on quantity" and mean it: the embedding
provider round-trips are the bottleneck, not this module.

## Rules

- One SQLite file, one connection, one lock; WAL mode. No search
  server, no external index — this must work offline on a desktop.
- Never embed on the query path except the query itself; chunk
  embedding is always the background thread's job.
- The sha key includes provider:model. Skipping this "works" until
  the first model switch silently mixes spaces and ranks garbage.
- Normalize before float16, not after read — do it once, at write.
- Quote FTS terms; user input is never a MATCH expression.
- Errors are sentences with a next step ("rebuild vectors in
  Settings"), not tracebacks and not empty lists.
- Everything is opt-in and visible: kind toggles, per-chat de-index,
  explicit code targets, explicit rebuilds with counts. An index the
  user can't see or control is a liability, not a feature.
