# Terminals

**Ctrl+T** opens a terminal tab: a real interactive bash on a PTY inside
one of your library's containers - the same sandbox the model's shell
tool uses, so what works for you works for it. The emulator handles
full-screen programs (nvim, less, htop), 256/truecolor, mouse support,
and scrollback.

## Setup

Each tab picks its **container** (from `containers:` in loom.yaml - the
image builds automatically on first use), optional **folder mounts**
(view = read-only, write = read-write, at `/mnt/<name>`), and a
**network mode** (off by default): *no network*, *loopback only* (the
host's `127.0.0.1` services reachable at `10.0.2.2`, nothing else -
podman/slirp4netns), or *network on*. A new terminal inherits the
setup of the active terminal tab - or, opened from anywhere else, of the
most recently used terminal - so "another shell like this one" is
Ctrl+T → Start.

Terminals can also load an **environment** - a named env-var set from
the Environments tab (key icon in the top bar, Ctrl+Shift+E): plain
values from the library's `environments.yaml` plus secrets from your OS
keyring land in the shell's environment, and missing secrets are called
out at startup so you know exactly what to set.

Once running, the header stays live: the **container selector** switches
the shell into another image, the **network pill** cycles the mode, and
the **environment selector** swaps env-var sets - any of them kills the
current container and starts a fresh shell with the new setup, but the
scrollback buffer is kept (carried history replays above the new shell).
If more than a bare shell is running, Loom lists the processes and asks
first.

## Keys, copy, paste

| | |
| --- | --- |
| Ctrl+Shift+C / Ctrl+Shift+V | copy selection / paste |
| right-click | copy / paste / select-all menu |
| Shift+PageUp / Shift+PageDown | scroll the buffer |
| Shift + mouse | select text even when a program owns the mouse |
| Enter (after exit) | restart the shell |

Everything else - Ctrl+C, Ctrl+R, Ctrl+W, arrows, F-keys - goes to the
shell. Selections survive output: the screen repaints around them.

## Program permissions

When a program requests a gated capability - alternate screen, mouse
reporting, bracketed paste, setting the tab title, reading or writing
your clipboard (OSC 52) - a prompt stacks in the corner: **Ctrl+Y**
allow, **Ctrl+N** deny, or click (Always/Never remember the answer).
Clipboard access defaults to *ask* because a program can use it
invisibly.

## Lifetime

Sessions live in the backend: switching tabs, closing the window to the
tray, and reopening all keep the shell running and replay the scrollback.
Closing the tab (or restarting the shell) kills it - if anything beyond
the bare shell is running, Loom lists the processes and asks first.
Terminals die with Loom; they can never outlive the app.
