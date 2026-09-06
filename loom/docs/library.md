# The library

A library is one ordinary folder that holds everything Loom knows for a
given context: configuration, prompts, knowledge, container recipes, your
chats, and these docs. Version it with git, sync it, copy it between
machines - it's just files. Loom opens exactly one library at a time;
switch from the top bar (running servers stop when you leave, after a
confirmation that lists them).

## Layout

```
loom.yaml            configuration - providers, chat defaults, permission
                     modes, containers, MCP servers, the API server
                     (loom.yml works too)
environments.yaml    env-var sets for shell containers: plain values +
                     secret STUBS only - secret values stay in your OS
                     keyring, never in the library
prompts/             every prompt as plain markdown: system.md,
                     compaction.md, title.md - edited live, used on the
                     next send
knowledge/           plain-markdown knowledge base, searchable from chats
containers/          Containerfiles named by loom.yaml definitions
documentation/       these docs - yours to edit like anything else
internals/           Loom's working data: chat transcripts and each
                     chat's artifacts (purged with the chat). Hidden in
                     the file tree; right-click → "Show hidden" reveals it
```

## The file tree (Library tab)

- Right-click anywhere for **new file / new folder**; right-click an
  entry for **rename / delete**. The right pane edits one file at a time
  and makes you save (Ctrl+S) or discard before navigating away.
- Markdown gets the decorated source editor (neon headings, highlighted
  fences, Ctrl+B/I/E wrappers); yaml and code files get syntax
  highlighting and **Ctrl+/** comment toggling.
- The search box fuzzy-matches file paths *and* content lines, ranked
  together.
- Dotfiles and `internals/` stay out of sight until you right-click the
  tree and toggle **Show hidden files**.

## loom.yaml is the steering wheel

Everything configurable lives in it - providers, chat defaults,
permission modes, container definitions, MCP servers, the API server.
It re-parses on every save; the Providers tab and open chats pick
changes up immediately. If it stops parsing, the Providers tab shows
the exact error until it parses again.

Two ways to edit it, both yours:

- **By hand**, here in the Library tab - the file is user-owned, and
  Loom's own edits never touch your comments or ordering.
- **The Configuration tab** (gear in the top bar): the same file in a
  validated editor - a save that would not parse is rejected with the
  exact error and nothing lands on disk, and a good save applies live
  (providers re-probe, a running API server rebinds if its address
  changed). Its dialogs - **Chat defaults…**, **Permission modes…**,
  **Containers…** - write minimal blocks and leave the rest of the
  file byte-for-byte; anything matching Loom's defaults stays out of
  the file entirely.

## Prompts are files, nothing more

`chat.system_prompt`, `chat.compaction_prompt`, and `chat.title_prompt`
name markdown files in this library. They are read fresh at each use -
edit them mid-conversation and the very next message uses the new text.

## The knowledge base

Anything under `knowledge/` is searchable from chats
(`knowledge_search`) and readable by the model - in chats it is also
mounted read-only at `/knowledge` for shell commands. Write documents as
practices anyone can follow - the same file serves whoever executes the
steps, human or model. Small focused files make precise search hits.
**[Curating knowledge](curating-knowledge.md)** covers the full craft:
what belongs, how to structure it, and how to do the gardening together
with a model.

## Templates

Creating a library offers a choice of templates: **Starter** (bare
essentials) and **Developer** (a phased engineering workflow plus a
Python/Go knowledge base and dev container), plus any folder you place
under `~/.loom/templates/<name>/` - a template is simply a library
skeleton that gets copied in, with an optional one-line `template.txt`
description shown in the picker.
