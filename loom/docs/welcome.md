# Welcome to Loom

Loom is a fully local workbench for running language models with
llama-server and working with them — chats with tools, sandboxed
terminals, and a knowledge base — all organized around one folder you
own: **this library**.

Nothing leaves your machines. The UI has no web listeners, the managed
servers bind unix sockets only, models can run on remote hardware over
plain ssh, and shell commands run inside containers as an unprivileged
user.

## Where to go next

- **[Quick start](quick-start.md)** — from zero to a first conversation,
  every click spelled out.
- **[The library](library.md)** — what this folder is, its layout, and
  how loom.yaml drives everything.
- **[Chats](chats.md)** — tools, permission modes, attachments, the
  context window and compaction.
- **[Terminals](terminals.md)** — real shells in containers, hotkeys,
  copy/paste.
- **[Models and servers](models-servers.md)** — finding GGUFs, the
  New-model wizard, pinning, reasoning effort, running servers locally
  and over ssh.
- **[Curating knowledge](curating-knowledge.md)** — how to grow a
  knowledge base worth searching, including doing it together with a
  model.
- **[Inference tips](inference-tips.md)** — llama-server flags, quants,
  context sizes, vision models, speculative decoding.

## The five-second tour

The top bar's buttons open everything: **chat bubble** new chat (Ctrl+N),
**terminal** (Ctrl+T), **book** the Library tab (these files live there —
Ctrl+L), **stack** the Servers tab (Ctrl+E), **⤓** the Models utility
(Ctrl+M), **box** the Chat Archive (Ctrl+H), **key** the Environments
tab (Ctrl+Shift+E). The bell opens the
**Alerts tab** (Ctrl+Shift+A): every toast lands there and stays —
across restarts — until you clear it, so nothing you're shown is lost
when a popup fades.

**Hold Ctrl** anywhere and every control on screen reveals its keyboard
shortcut in a small chip — that's the whole cheat sheet. Ctrl+Shift+I
opens real Chromium DevTools if you like looking under the hood.

Close the window and Loom keeps running in the system tray — servers and
terminals stay up. Quit from the tray menu when you mean it.
