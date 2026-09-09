# Curating knowledge

The knowledge base is the difference between a model that guesses and a
model that follows your practices. It rewards gardening: a small,
current, well-shaped `knowledge/` beats a big stale one every time,
because search hits land on exactly one authoritative answer instead of
three contradicting drafts.

## What belongs (and what doesn't)

**Belongs**: practices, decisions, and reference the next conversation
should not have to rediscover - how deploys work here, the API quirks
that bit you, the style rules that matter, the checklist for a release.
Write each as instructions anyone could follow; the same file serves
whoever executes the steps, human or model.

**Doesn't**: anything derivable from the attached code (the model can
read that), chat transcripts (distill the lesson out of them instead),
secrets and credentials (the knowledge base is readable by every chat),
and anything you aren't willing to keep true - a wrong document is
worse than none, because it gets cited with confidence.

## Shape it for search

- **Small, focused files** - one topic per file. `knowledge_search`
  returns file-and-line hits; a 40-line document about one thing is a
  precise hit, a 600-line "everything" file is noise.
- **READMEs as maps** - a `README.md` in each folder saying what lives
  where. The model is told to read them first when they match; they are
  the folder's table of contents.
- **Names that match questions** - `postgres-migrations.md` gets found
  by someone asking about migrations; `notes3-final.md` never is.
- **State the expiry** - when a practice is version-bound ("as of
  llama.cpp b6000…"), say so in the first lines, so staleness is
  visible at the moment of citation.

## The curation loop

1. **Capture** - when a conversation produces a hard-won answer, ask
   the model to write it up as a knowledge document before you close
   the chat (it can deliver a draft as an artifact for your review, or
   write it directly - next section).
2. **Distill** - practices, not transcripts. "Do X, then Y, because Z"
   survives; "we discussed maybe trying X" doesn't.
3. **File** - put it where the READMEs say it goes; update the README
   when a new area appears.
4. **Audit** - every so often, sweep for contradictions, duplicates,
   and stale versions. This is the part worth delegating (next
   section).

## Curating together with a model

The model is a good librarian for its own library - and this is a
first-class workflow:

1. **Drag the book icon** from the top bar into a chat's message area.
   The whole library attaches **read-only** - the model can search,
   read, and cross-reference every document, but change nothing. This
   mode is perfect for audits:
   > "Read every document under knowledge/ (skim via the READMEs
   > first). List: duplicates, contradictions between documents, files
   > that state no expiry but look version-bound, and folders whose
   > README doesn't match their contents. Propose a reorganization -
   > deliver the plan as a markdown artifact."
   The proposal arrives as an artifact you can read and keep before
   anything changes.
2. **Flip the library pill to `write`** when you want the edits made.
   Now `edit_file`/`write_file`/`shell` can modify the knowledge base
   (and prompts) - under whatever permission mode you choose:
   `always-ask` shows you every diff before it lands (the permission
   card renders the real diff), `allow-edits` lets a trusted
   reorganization run unattended.
3. **Let git be the safety net.** If the library is a git repo (worth
   doing), the attachment pill shows the checked-out branch - cut a
   `knowledge-gardening` branch first, let the model work, review the
   diff, merge. The model can run `git diff` in the shell for its own
   review, too.
4. **Iterate in small passes.** "Dedupe these two folders" beats
   "reorganize everything" - small passes are reviewable, and each one
   updates the READMEs so the next pass starts from an honest map.

Prompts that work well: *"add what we just learned about X to the
knowledge base where it belongs, updating the folder README"*, *"find
every document that mentions Y and reconcile them into one"*, *"write
the missing README for knowledge/deploys/"*.

The same drag-and-write flow also lets the model improve its own
`prompts/` - the system prompt is a file like any other, and prompt
edits apply from the very next message.

## Keep it honest

- The model is told to **cite what it reads** and to say when the base
  is empty rather than invent citations - keep that true by deleting
  wrong documents, not just superseding them.
- After a big curation pass, spot-check by asking a fresh chat a
  question the base should answer - the right document should come back
  as the hit.
