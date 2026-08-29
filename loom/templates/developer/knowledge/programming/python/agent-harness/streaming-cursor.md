# Streaming rendering — live markdown, the cursor, and patch-not-rebuild

Streamed model output must LOOK live: markdown that reflows as tokens
arrive, a blinking cursor riding the end of the text, thoughts that
stay open while the model reasons, tool cards that update in place —
all without eating the user's clicks, focus, or text selection. The
backend side is trivial (push a `delta {text}` event per streamed
chunk — agent-loop.md defines the vocabulary, ui-bridge.md the
delivery); everything hard is on the page. This file is the frontend
recipe, verified pitfalls included.

## Accumulate on the message, re-render from the full text

Never append DOM per delta. Each streaming span is a message object
(`{role: "assistant", text: "", streaming: true}`); a delta does
`m.text += ev.text`, then the visible bubble is re-rendered from the
FULL accumulated text:

```js
body.innerHTML = renderMarkdown(m.text);
sanitizeChatLinks(body, chat);
appendCursor(body);
```

Re-parsing the whole message per delta is fine — only the last bubble
is touched, and markdown parsers are fast at chat-message sizes. The
payoff: half-open constructs (an unclosed code fence, a table mid-row)
always render as the parser's best current interpretation and snap
correct when the closing token arrives. Parsing partial markdown is
safe; SANITIZING it is mandatory (below).

## The markdown pipeline

Two 3rd-party libraries, vendored locally (no CDN):

```js
function renderMarkdown(src) {
  const html = marked.parse(src ?? "", { gfm: true, breaks: false });
  return DOMPurify.sanitize(html);
}
function renderThoughtMarkdown(src) {   // breaks: true — see below
  return DOMPurify.sanitize(marked.parse(src ?? "",
    { gfm: true, breaks: true }));
}
```

DOMPurify runs on EVERY render, partial text included — model output
is untrusted HTML the moment marked converts it. Thoughts get
`breaks: true` because reasoning models separate thought paragraphs
with single `\n`; under normal markdown rules those collapse into one
unreadable wall of text.

After every innerHTML set, repair links: validate `#/`-style hrefs
against the app's real route table, rewrite recognizable hallucinated
hrefs (a bare commit sha → the commit page, a path-looking href → the
file on the chat's branch), and demote the rest to literal text — a
model-invented link must never render as a dead clickable.

## The cursor

The cursor is a `<span class="cursor">` (CSS blink) injected at the
END of the streamed text — INLINE after the last character, never
after the markdown blocks where it would sit on a line of its own.
Walk into the deepest trailing block-level element and append there:

```js
function appendCursor(el) {
  const BLOCKS = new Set(["P","LI","UL","OL","BLOCKQUOTE","H1","H2",
    "H3","H4","H5","H6","PRE","CODE","TD","TH","TABLE","THEAD",
    "TBODY","TR"]);
  let host = el;
  for (;;) {
    let last = host.lastChild;
    while (last && ((last.nodeType === Node.TEXT_NODE
                     && !last.textContent.trim())
                    || last.nodeType === Node.COMMENT_NODE))
      last = last.previousSibling;          // skip "\n" between blocks
    if (last?.nodeType === Node.ELEMENT_NODE && BLOCKS.has(last.tagName))
      { host = last; continue; }
    break;
  }
  if (host.tagName === "CODE"
      && host.lastChild?.nodeType === Node.TEXT_NODE)
    host.lastChild.textContent =
      host.lastChild.textContent.replace(/\n$/, "");
  host.appendChild(h("span.cursor"));
}
```

Two verified gotchas live in that function:

- Markdown renderers emit `"\n"` TEXT nodes between blocks, so "last
  child" must mean last MEANINGFUL child — skip whitespace-only text
  and comment nodes, or the descent stops at the container and the
  cursor lands on its own line.
- Code fences keep a trailing newline inside `<code>`; appending the
  cursor after it drops it a line down INSIDE the box. Trim exactly
  that one `\n` — the next delta re-renders from source anyway, so
  nothing is lost.

A turn that is streaming but has produced no text yet still shows an
empty bubble containing only the cursor — silence with a heartbeat.

## Thoughts stay open and cursored

Reasoning arrives two ways: structured `thought` events, or inline
`<think>…</think>` tags in the text (common with local reasoning
models). Split the text into ordered thought/text segments; an
UNCLOSED `<think>` mid-stream runs to the end of the text and renders
as an open, cursored thought block. While a thought streams, skip
markdown: set `textContent` with `white-space: pre-wrap` and append
the cursor — plain text keeps per-tick updates cheap; the block gets
its markdown render once it closes. Closed thoughts collapse to a
"thoughts (N tokens)" line, click to expand.

## Patch, don't rebuild

The renderer has two paths. A STRUCTURAL change (new message, tool
card, status flip) rebuilds the thread. A stream tick takes the patch
path: snapshot `{thread el, message count, status}` when the view is
built; on each tick, if count and status are unchanged, patch in
place — update the last bubble's innerHTML + cursor, tick the token
counter, swap the context meter, autoscroll — and return. Any
structural drift makes the patch report failure and the caller does a
full render. Specific patch rules that took debugging to learn:

- If the streaming text contains `<think>`, bail to a full render:
  the segment layout (thought blocks interleaved with bubbles) can
  change shape mid-stream, and patching the wrong node corrupts it.
- Sidebar/list panels are rebuilt ONLY when a signature of their
  visible structure (order, status dots, selection) changes.
  Rebuilding them on every streamed token kills rows under the
  cursor and eats clicks.
- Tool events arrive in bursts; coalesce their renders on a ~200 ms
  throttle. An immediate full render per call/result starves the
  main thread AND destroys nodes mid-click. Live shell output appends
  to its card through the same throttle, capped (e.g. last 40 kB).
- Permission "ask" renders IMMEDIATELY, bypassing the throttle — a
  question for the user must not lag.

Park renders during interaction: a rebuild between mousedown and
mouseup destroys the node under the pointer and the browser silently
drops the click; a rebuild also closes open context menus and clears
text selections. Track pointer-held / menu-open flags, queue the
refresh ("full" outranks "light"), and replay it one macrotask after
release — the click dispatches first, on a still-live target. Also
reset the flag on window blur, or a drag out of the window parks
every future render. Inputs that must survive full rebuilds (the
composer, find-in-chat) save value/focus/caret before `render()` and
restore after.

## Scroll behavior

Stick-to-bottom, user-in-control: autoscroll after each patch only
while the user IS at the bottom (within ~48 px). Scrolling up
disengages — reading history stays put through streams and rebuilds —
and a floating "Latest" button jumps down and re-engages. On a full
rebuild, restore the saved scroll position when disengaged; when
sticking, scroll again ~150 ms later to catch late height changes
(font load, row wrapping).

## Frontend stream state machine

The event handlers keep three live handles per chat: the streaming
assistant message, the streaming thought message, and the last tool
card by callId. `thought` opens (or grows) the thought; `delta`
closes any open thought and opens/grows the bubble; `turn_break` and
`tool_call` freeze the bubble (set `streaming = false`, stamp the end
time — the timing stamps back tok/s and TTFT diagnostics); `done`,
`error`, and `retry` freeze both. Persist the transcript per
tool-result and on done — never per token.

## Rules

- Re-render streamed markdown from the full accumulated source;
  never append raw HTML fragments per delta.
- DOMPurify after EVERY marked.parse — partial or complete, message
  or thought. No exceptions.
- Cursor injection is a DOM walk to the deepest trailing block,
  skipping whitespace/comment nodes; trim the code-fence trailing
  newline before appending.
- `<think>` in streaming text → full render, not patch.
- Rebuild lists on structure signatures, not on ticks; throttle
  bursty tool/shell renders ~200 ms; render permission asks
  immediately.
- Park renders while the pointer is down or a menu is open; replay
  after — and clear the flag on window blur.
- Autoscroll only while stuck to the bottom; never teleport a user
  who scrolled up.
- Keep marked + DOMPurify vendored and pinned; the renderer is a
  security boundary (agent-loop.md emits the events, ui-bridge.md
  delivers them in order, tool-catalog.md shapes the tool cards'
  params/diff payloads).
