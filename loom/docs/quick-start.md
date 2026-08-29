# Quick start

From a fresh library to a first working conversation. Every step is a
click or an edit; nothing is assumed.

## 1. Get a model file

You need a GGUF on disk (llama.cpp's model format). If LM Studio or a
Hugging Face download already put one somewhere, skip to step 2 — the
scanner will find it.

1. Click the **⤓ Models** button in the top bar.
2. Paste a direct GGUF URL into the download field (on Hugging Face:
   open the file page, copy the *download* link — it contains
   `/resolve/`), press **Download**.
3. Watch the progress row; the file lands in `~/.loom/models/`.

## 2. Turn the model into configuration

The **New model… wizard** does this in three quick steps. It lives in
the Servers tab (Ctrl+E) — and the chat's model menu offers it too when
no models exist yet.

1. **Pick**: choose where llama-server runs (localhost, or an ssh host
   chip) and click the GGUF in the scanned list — picking it moves on.
   If an `mmproj` (vision projector) sits beside it, it pairs
   automatically.
2. **Shape**: name it, size the context with the slider (it starts at
   the model's trained maximum), pick the backend, keep or drop the
   vision projector. **Review →**.
3. **Review**: the exact yaml, editable in place. **Add to loom.yaml**
   appends it non-destructively — your comments and ordering elsewhere
   stay untouched.

## 3. Start the server

1. Click the **stack (Servers)** button in the top bar.
2. Your model appears as a card. Click **Start**.
3. The dot turns amber while the model loads (big models take a while —
   the card shows progress), then green: running. If it errors, click
   **Log** — the real llama-server output tells you why (wrong path,
   out of memory, bad flag).

## 4. Talk

1. Press **Ctrl+N** (or the **＋** button) for a new chat.
2. Type a message and press **Enter**.
   - If you skipped step 3, Loom offers to start the server for you and
     sends the message the moment the model is ready — it waits in the
     visible queue above the input meanwhile.
3. Watch the reply stream. The chip on the input panel shows context
   usage — hover it for the full breakdown.

## 5. Give it something to work on (optional)

1. Click the **folder** button in the input panel and pick a project
   folder. It attaches read-only ("view") — the model can `grep`, read,
   and run shell commands over it, but the container mounts it
   read-only, so nothing can change. Git folders show their checked-out
   branch on the pill.
2. Click the pill's **view** label to flip it to **write** when you
   want edits. All of it runs inside a container, never on your host,
   asking permission according to the mode in the shield pill.
3. Anything the model saves to `/artifacts` comes back to you as an
   attachment pill — files save directly, folders as zip downloads.
   Your uploaded images land in `/artifacts/uploads` for it to process.
4. Tip: drag the **book (Library)** icon from the top bar into the
   message area to attach this whole library read-only — see
   [curating knowledge](curating-knowledge.md) for why that's useful.

## 6. A terminal, when you want your own shell

1. Press **Ctrl+T**.
2. Pick a container, optionally mount folders, click **Start shell**.
3. It's a real shell in the same sandbox the model uses — nvim, builds,
   whatever fits. Ctrl+Shift+C / Ctrl+Shift+V copy and paste.

That's the loop: models in `loom.yaml`, servers in the Servers tab,
work in chats and terminals, knowledge and prompts in this library.
