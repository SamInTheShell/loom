"""Chat persistence — one JSON file per chat in <library>/chats/.

A chat file: {id, title, createdTs, updatedTs, archived, model,
folders: [{path, mode}], messages: [...]}.

`archived` is the tab-close semantic: closing a chat tab flips it true;
the Chat Archive tab lists archived chats and re-opening one flips it
back. On library open, chats with archived=false are the tabs to restore
— a quit with chats open resumes where the user left off.

Message shapes mirror the wire (OpenAI-style role/content) plus UI-only
fields the loop emits (thoughts, tool cards); chat.py owns those.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from loom import library


class ChatError(Exception):
    pass


def chats_dir(root: Path) -> Path:
    d = root / library.INTERNALS / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(root: Path, chat_id: str) -> Path:
    p = library.safe_join(root, f"{library.INTERNALS}/chats/{chat_id}.json")
    return p


def new_chat(root: Path, model: str = "") -> dict:
    chat = {
        "id": uuid.uuid4().hex[:12],
        "title": "New chat",
        "createdTs": int(time.time() * 1000),
        "updatedTs": int(time.time() * 1000),
        "archived": False,
        "model": model,
        "folders": [],
        "images": [],
        "messages": [],
    }
    save_chat(root, chat)
    return chat


def load_chat(root: Path, chat_id: str) -> dict:
    p = _path(root, chat_id)
    try:
        chat = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ChatError(f"no such chat: {chat_id}")
    if not isinstance(chat, dict) or chat.get("id") != chat_id:
        raise ChatError(f"chat file is damaged: {chat_id}")
    chat.setdefault("messages", [])
    chat.setdefault("folders", [])
    return chat


def save_chat(root: Path, chat: dict) -> None:
    chat["updatedTs"] = int(time.time() * 1000)
    p = _path(root, str(chat["id"]))
    chats_dir(root)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(chat, indent=1), encoding="utf-8")
        tmp.replace(p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def list_chats(root: Path) -> list[dict]:
    """Metadata only, newest first."""
    out = []
    d = chats_dir(root)
    for p in d.glob("*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(c, dict) or not c.get("id"):
            continue
        out.append({"id": c["id"], "title": c.get("title") or "Untitled",
                    "archived": bool(c.get("archived")),
                    "model": c.get("model") or "",
                    "updatedTs": c.get("updatedTs") or 0,
                    "messages": len(c.get("messages") or [])})
    out.sort(key=lambda c: -(c.get("updatedTs") or 0))
    return out


def purge_empty_archived(root: Path) -> int:
    """Delete archived chats with no messages — dead weight from tabs that
    were opened and abandoned before this rule existed."""
    n = 0
    for c in list_chats(root):
        if c["archived"] and c["messages"] == 0:
            delete_chat(root, c["id"])
            n += 1
    return n


def set_archived(root: Path, chat_id: str, archived: bool) -> dict:
    chat = load_chat(root, chat_id)
    chat["archived"] = bool(archived)
    save_chat(root, chat)
    return chat


def artifacts_dir(root: Path, chat_id: str, create: bool = False) -> Path:
    """The chat's artifact folder (mounted at /artifacts in shells).
    Holds user uploads and whatever the model leaves for the user."""
    d = library.safe_join(root, f"{library.INTERNALS}/chats/artifacts/{chat_id}")
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def delete_chat(root: Path, chat_id: str) -> None:
    p = _path(root, chat_id)
    p.unlink(missing_ok=True)
    # a permanent delete takes the chat's artifacts with it
    shutil.rmtree(artifacts_dir(root, chat_id), ignore_errors=True)
