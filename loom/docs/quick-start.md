# Quick start

From a fresh library to a first working conversation. Every step is a
click or an edit; nothing is assumed.

## 1. Run an inference server

Loom talks to an inference server you run yourself, or a hosted
vendor account:

- **llama.cpp**: `llama-server -m your-model.gguf --port 8080 --jinja`
  (`--jinja` enables tool calling - Loom's agent features need it).
  The server can be on this machine or any box you can ssh into with a
  key.
- **Hosted**: OpenAI, Anthropic, Gemini, Vertex AI, or Bedrock, with
  your API key (new and lightly tested - feedback welcome). See [inference tips](inference-tips.md) for flags worth knowing.

## 2. Point Loom at it

1. Open the **Providers** tab (Ctrl+E) and click **Add provider…**
2. Name it, pick the type, enter the URL
   (`http://127.0.0.1:8080`). If it runs on another machine, put the
   ssh destination in the SSH field - the URL is then resolved from
   that machine over an ssh tunnel (keys only; load one into
   ssh-agent).
3. **Test** probes it and lists what it serves; **Add to loom.yaml**
   writes the entry without touching anything else in the file.

The provider card now shows its models, each with its context window.

## 3. Talk

1. Press **Ctrl+N** (or the **＋** button) for a new chat.
2. The model button (bottom right of the input panel, Ctrl+.) picks
   provider → model → reasoning; with one provider and one model it's
   already right.
3. Type a message and press **Enter**. Watch the reply stream - live
   tok/s next to the cursor, and a real progress readout while the
   server reads your prompt. The chip on the input panel shows context
   usage - hover it for the full breakdown, including how long your
   next prompt should take to process.

## 4. Give it something to work on (optional)

1. Click the **folder** button in the input panel and pick a project
   folder. It attaches read-only ("view") - the model can `grep`, read,
   and run shell commands over it, but the container mounts it
   read-only, so nothing can change. Git folders show their checked-out
   branch on the pill.
2. Click the pill's **view** label to flip it to **write** when you
   want edits. All of it runs inside a container, never on your host,
   asking permission according to the mode in the shield pill.
3. Anything the model **delivers** (its deliver_artifact tool) comes
   back to you as an attachment pill - files save directly, folders as
   zip downloads. Your uploaded images land at `/uploads` (read-only)
   for it to process.
4. Tip: drag the **book (Library)** icon from the top bar into the
   message area to attach this whole library read-only - see
   [curating knowledge](curating-knowledge.md) for why that's useful.

## 5. A terminal, when you want your own shell

1. Press **Ctrl+T**.
2. Pick a container, optionally mount folders, click **Start shell**.
3. It's a real shell in the same sandbox the model uses - nvim, builds,
   whatever fits. Ctrl+Shift+C / Ctrl+Shift+V copy and paste.

That's the loop: providers in `loom.yaml`, models from their APIs,
work in chats and terminals, knowledge and prompts in this library.
