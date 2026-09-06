"""The chat-mirror terminal: a shell in the EXACT container setup a chat
uses, for diagnosing the environment the way the model sees it.

One session per chat (sid "chat-<id>"), hosted in a popped-out OS window
(app.chat_term_popout). The setup is computed HERE from the chat
document - the same fields chat.py's shell tool reads - never copied by
the frontend, so it cannot drift: image, /mnt folder mounts, the
/knowledge and /artifacts cuts, the chat's own /home/loom, network mode
and environment. envHidden never matters - hiding only trims the
model's prompt, the variables still load into containers.

The chat setters in app.py call sync_async after every save that touches
the container view; when a LIVE session's setup differs from the chat's,
the shell restarts with the new setup (scrollback carried, same as the
terminal tabs' switch semantics).
"""

from __future__ import annotations

import threading
from pathlib import Path

from loom import chats, containers, terminals

_LOCK = threading.Lock()
_SIDLOCKS: dict[str, threading.Lock] = {}   # serialize open/sync per chat
_SETUP: dict[str, dict] = {}                # sid -> setup the shell runs
_DIMS: dict[str, tuple[int, int]] = {}      # sid -> (cols, rows)


def sid_for(chat_id: str) -> str:
    return "chat-" + str(chat_id)


def _sid_lock(sid: str) -> threading.Lock:
    with _LOCK:
        return _SIDLOCKS.setdefault(sid, threading.Lock())


def setup_for(root: Path, chat: dict) -> dict:
    """The terminal setup a chat's shell tool would use, comparable
    across calls (paths as strings)."""
    cid = str(chat["id"])
    return {
        "container": str(chat.get("container") or ""),
        "folders": [{"path": str(f.get("path") or ""),
                     "mode": str(f.get("mode") or "view")}
                    for f in chat.get("folders") or []],
        "network": containers.net_mode(chat.get("network")),
        "env": str(chat.get("env") or ""),
        "knowledge": None if chat.get("knowledgeOff")
        else str(root / "knowledge"),
        "artifacts": None if chat.get("artifactsOff")
        else str(chats.artifacts_dir(root, cid, create=True)),
        "home": str(containers.chat_home(cid)),
    }


def _open(push, root: Path, chat_id: str, setup: dict,
          cols: int, rows: int) -> None:
    sid = sid_for(chat_id)
    with _sid_lock(sid):
        with _LOCK:
            _SETUP[sid] = setup
            _DIMS[sid] = (cols, rows)
        terminals.open_session(
            push, root, sid, setup["container"], setup["folders"],
            setup["network"], cols, rows, env_name=setup["env"],
            knowledge=Path(setup["knowledge"]) if setup["knowledge"]
            else None,
            artifacts=Path(setup["artifacts"]) if setup["artifacts"]
            else None,
            home=Path(setup["home"]))


def open_for_chat(push, root: Path, chat_id: str,
                  cols: int = 120, rows: int = 32) -> None:
    """Start (or restart) the chat's mirror shell. Blocking - image
    builds take a while; call on a worker thread."""
    chat = chats.load_chat(root, str(chat_id))
    _open(push, root, str(chat_id), setup_for(root, chat), cols, rows)


def sync(push, root: Path, chat_id: str) -> bool:
    """The chat's container view changed on disk - restart a LIVE mirror
    shell if its setup no longer matches. True = a restart happened."""
    sid = sid_for(chat_id)
    if not terminals.alive(sid):
        return False
    try:
        chat = chats.load_chat(root, str(chat_id))
    except chats.ChatError:
        return False
    want = setup_for(root, chat)
    with _LOCK:
        have = _SETUP.get(sid)
        cols, rows = _DIMS.get(sid, (120, 32))
    if have == want:
        return False
    terminals._emit(push, sid, "line",
                    text="[loom] the chat's setup changed - "
                         "restarting shell to match…")
    _open(push, root, str(chat_id), want, cols, rows)
    return True


def sync_async(push, root: Path, chat_id: str) -> None:
    """Fire-and-forget sync - the setters must never block on a shell
    restart (or an image build)."""
    if not terminals.alive(sid_for(chat_id)):
        return
    threading.Thread(target=lambda: sync(push, root, str(chat_id)),
                     daemon=True, name=f"chatterm-sync-{chat_id}").start()


def close_for_chat(chat_id: str) -> None:
    sid = sid_for(chat_id)
    # the sid lock lets an in-flight open/sync finish REGISTERING its
    # session first - closing mid-open would orphan the fresh container
    with _sid_lock(sid):
        terminals.close_session(sid)
        with _LOCK:
            _SETUP.pop(sid, None)
            _DIMS.pop(sid, None)
