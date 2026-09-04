# Attachments & extraction - staging user files for a model, safely

Users drop arbitrary files into a chat; the model needs their content
without the app ever executing, rendering or trusting those files. The
answer is a staging step that sorts every file into one of three kinds
at attach time, a parse-only extraction module with a cap on every
axis, and one invariant that pays for itself forever: a chat message
stays JSON-serializable and reasonably small - text rides inline,
binaries ride as filenames into a per-chat directory, and base64 is
produced only at send time. The same extraction is reused when the
agent's own tools (tool-catalog.md) read a document or image out of a
repository or attached directory, so "a repo full of scanned finance
PDFs" is readable by a local model with zero extra machinery.

## Three kinds, decided per file at staging

For each picked path, in extension order:

1. image (`.png .jpg .jpeg .gif .webp`) - validate MAGIC BYTES (an
   extension is a claim, not a fact), cap at 10 MB, copy the FILE into
   `~/.yourapp/attachments/<chat>/` under a generated name
   (`att-<10 hex>.<ext>`); only that name rides on the message.
2. doc (`.pdf .docx .xlsx .pptx .odt .ods .odp`) - cap source at
   25 MB, run safe extraction (below); the extracted TEXT is stored
   inline on the message like a text file.
3. everything else - cap at 2 MB, reject if a NUL byte appears in the
   first 8000 bytes (binary), else decode UTF-8 with `errors=
   "replace"` and store inline, truncated at 80 000 chars with an
   honest note.

Any per-file failure becomes a `{kind:"error", error:…}` record - one
bad file never sinks the batch, and the UI shows exactly which file
failed and why. The record shapes:

```python
{"id": "att-…", "name": n, "kind": "text",  "text": …, "chars": …}
{"id": "att-…", "name": n, "kind": "doc",   "format": "pdf",
 "text": …, "note": …}
{"id": "att-…", "name": n, "kind": "image", "mime": "image/png",
 "file": "att-….png", "width": w, "height": h}
```

## Safe document extraction - parse, never execute or render

One module, one entry point `extract_text(filename, data) -> dict`,
which NEVER raises: every path returns
`{"ok", "text", "format", "note", "truncated"}` and a malformed file
becomes `ok:False` with the reason in `note`. Caps, all enforced:

```python
MAX_SRC_BYTES    = 25 * 1024 * 1024   # refuse bigger sources
MAX_TEXT_CHARS   = 80_000             # output cap per file
MAX_PDF_PAGES    = 400
MAX_ZIP_MEMBERS  = 4096
MAX_MEMBER_BYTES = 50 * 1024 * 1024   # decompressed, per member
MAX_IMAGE_BYTES  = 10 * 1024 * 1024
```

PDF: pypdf's text extractor ONLY - embedded JavaScript, forms and
attachments are simply never touched because no code path opens them.
Encrypted files: try `decrypt("")` (empty owner password) and
otherwise refuse as password-protected. Emit `--- page N ---` headers,
stop past the page or char cap, and when the result is effectively
empty, say so: "no extractable text (scanned/image-only PDF? OCR is
not performed)" - an honest note beats a silent blank.

Office/OpenDocument files are zip archives of XML. Open with
`zipfile`, refuse archives with more than `MAX_ZIP_MEMBERS` entries,
and read each member with `f.read(MAX_MEMBER_BYTES + 1)` - one byte
over the cap is an error, which is the whole zip-bomb defense. Parse
XML with `defusedxml.ElementTree` (entity-expansion safe); macros,
OLE objects and embedded media are never opened. Match tags by local
name (`tag.rsplit("}", 1)[-1]`) so namespace prefixes don't matter.
Per format:

- docx → paragraphs from `word/document.xml` (`p`/`t`, `br`/`cr` as
  newlines).
- pptx → `ppt/slides/slideN.xml` sorted by `(len(name), name)` so
  slide2 precedes slide10; `--- slide N ---` headers.
- xlsx → resolve `xl/sharedStrings.xml` for `t="s"` cells, handle
  `inlineStr`, sheet names from `xl/workbook.xml`; tab-separated
  cells, note that formulas appear as their last computed value.
- odt/odp → text of `p`/`h` nodes via `itertext()` - ODF scatters
  text into tails of nested spans and itertext is the only walk that
  collects all of it. ods → tables/rows/cells the same way.

## Image validation - headers only, no decoder

`image_info(filename, data)` checks magic bytes and reads dimensions
straight from the header - no image decoder runs on untrusted bytes:
PNG (`\x89PNG\r\n\x1a\n`, IHDR width/height), JPEG (`\xff\xd8\xff`,
scan segment markers for a SOFn frame), GIF (`GIF87a/89a`), WebP
(`RIFF….WEBP`, dimensions left 0). Content that doesn't match the
extension returns None and staging rejects it with "not a valid PNG
file (content does not match the extension)".

WebP and GIF are transcoded to PNG at staging. The reason is
empirical: local OpenAI-compatible servers (Ollama's `/v1`, LM
Studio) whitelist `image/png` and `image/jpeg` data URIs and answer
HTTP 400 to anything else. Use the GUI toolkit's image class off the
UI thread if the app has one (Qt's QImage is thread-safe where
QPixmap is not; an animated GIF contributes its first frame, which is
what a model should see anyway); if the toolkit is unavailable
(headless tests), keep the original bytes - cloud providers accept
them. For images staged before transcoding existed, transcode on the
fly at send time for OpenAI-compat providers, and on transcode
failure send the original anyway: trying beats silently dropping the
image.

## The JSON invariant and send-time assembly

Messages persist as plain JSON (persistence.md): inline text for
text/doc kinds, a filename for images. Exports keep the extracted
text; nothing in the transcript is a blob. At send time
(llm-providers.md):

- text/doc attachments become tagged blocks appended to the user
  text: `<attachment name="…" type="pdf" note="…">…</attachment>`,
  clipped to a context-proportional budget - roughly 20 % of the
  model's context window in chars (`tokens * 0.2 * 4`), clamped to
  4 KB-200 KB, 24 KB when the window is unknown - with the clip noted
  in the tag.
- images are read from the staging dir and become provider-native
  parts: `image_url` data URIs for OpenAI-compat, raw base64 blocks
  for Anthropic/Gemini. A missing file becomes visible text
  ("[attached image X is no longer available]"), and an
  attachment-only message gets "(see attached image)" so no provider
  receives an empty string.

Staged filenames are confined: reject anything containing `/` or `\`
or starting with `.` before joining onto the chat's directory. The
chat-id path segment is sanitized (`[^a-zA-Z0-9._-]` → `_`, 80-char
cap). Deleting a chat deletes its attachment directory (`rmtree`
with `ignore_errors`, wired into chat deletion, never raises) - the
per-chat layout exists precisely so cleanup is one rmtree.

## Tool reads reuse the same machinery

The agent's `read_file` (tool-catalog.md), against a repository
branch or an attached directory, routes by the same extension sets:

- Document extensions run `extract_text` on the blob and serve the
  text with a header line: `[extracted text from pdf - formatting/
  images not included]` - so a model can read a repo of PDFs like
  source files.
- Image extensions return the raw bytes on the tool RESULT as
  `image: {mime, b64}`, which the chat layer delivers as real
  provider-native image content next to the textual result. A model
  without vision errors visibly - same honesty rule as message
  attachments; never downgrade an image to a filename silently.
- Content grep is extended to documents: `git grep -I` skips binary
  files, so grep the SAME extracted text `read_file` serves (header
  line included - match line numbers feed straight into read_file's
  `start_line`). Cache extraction by blob sha (content-addressed,
  immutable, so the cache never goes stale), bounded: ~48 cached
  docs, 50 docs extracted per grep call, 20 hits per file.

The search index chunks text/doc attachment content along with the
message it rides on (search-index.md), so attached documents are
findable later.

## Rules

- Parse, never execute or render: no PDF JS/forms/attachments, no
  Office macros/OLE, no image decoders on untrusted bytes at staging.
- Cap every axis - source bytes, zip members, decompressed bytes,
  pages, output chars - and read cap+1 to detect overflow instead of
  trusting archive metadata.
- Extraction entry points return error results; they never raise. A
  malformed file must not break the caller, and the note must name
  the file and the reason.
- Magic bytes decide what a file is; the extension only decides which
  validator to try.
- Messages stay JSON-serializable and small: base64 exists only in
  flight, never at rest in a transcript.
- Confine every filename that touches the staging directory; build
  paths from generated names, not user input.
- Honest degradation everywhere: truncation notes, "no OCR" for
  scanned PDFs, visible errors for vision-less models and missing
  files. Silence is the only unacceptable failure mode.
