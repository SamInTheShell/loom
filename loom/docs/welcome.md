# Welcome to Loom

Loom is a local-first workbench for working with language models -
chats with tools, sandboxed terminals, and a knowledge base - all
organized around one folder you own: **this library**. Inference runs
wherever you run it: Loom talks to llama.cpp's `llama-server` over
its HTTP API (directly or through an ssh tunnel), and to hosted
vendors - OpenAI, Anthropic, Gemini, Vertex AI, Bedrock.

The UI has no web listeners, remote providers are reached over plain
ssh (key auth only), and shell commands run inside containers as an
unprivileged user.

## Where to go next

- **[Quick start](quick-start.md)** - from zero to a first conversation,
  every click spelled out.
- **[The library](library.md)** - what this folder is, its layout, and
  how loom.yaml drives everything.
- **[Chats](chats.md)** - tools, permission modes, attachments, the
  context window and compaction; copying, forking, and pruning the
  history; auto-continue.
- **[Terminals](terminals.md)** - real shells in containers, hotkeys,
  copy/paste.
- **[Providers and models](models-servers.md)** - pointing Loom at
  llama-server or a hosted vendor, ssh tunnels, pinning, reasoning effort.
- **[Curating knowledge](curating-knowledge.md)** - how to grow a
  knowledge base worth searching, including doing it together with a
  model.
- **[Inference tips](inference-tips.md)** - llama-server flags, quants,
  context sizes, vision models, speculative decoding.

## The five-second tour

The top bar's buttons open everything: **chat bubble** new chat (Ctrl+N),
**terminal** (Ctrl+T), **book** the Library tab (these files live there -
Ctrl+L), **stack** the Providers tab (Ctrl+E), **box** the Chat Archive
(Ctrl+H), **key** the Environments tab (Ctrl+Shift+E), the **MCP glyph**
the MCP Servers tab (tool servers for chats), **globe** the API Server
tab (serve every provider's models back out as one OpenAI-compatible
API), and **gear** the Configuration tab - the whole loom.yaml in a
validated editor, plus dialogs for chat defaults, permission modes, and
containers. The bell opens the **Alerts tab** (Ctrl+Shift+A): every
toast lands there and stays - across restarts - until you clear it, so
nothing you're shown is lost when a popup fades.

Curious what a chat actually cost? The context chip's hover card opens
**Diagnostics** - per-turn tokens, tok/s, prefill speed, cache hits,
and a graph over the whole conversation, all updating live as the chat
runs. The **⇱ Pop out** button moves it into its own window (it keeps
following the chat, and closes with the main window); **⇤ Return to
app** brings it back as a tab.

**Hold Ctrl** anywhere and every control on screen reveals its keyboard
shortcut in a small chip - that's the whole cheat sheet. Ctrl+Shift+I
opens real Chromium DevTools if you like looking under the hood.

Close the window and Loom keeps running in the system tray - chats and
terminals stay up. Quit from the tray menu when you mean it. Your
inference servers are your own processes either way; Loom never
touches them.
