"""Chat persistence - one JSON file per chat in <library>/chats/.

A chat file: {id, title, createdTs, updatedTs, archived, model,
folders: [{path, mode}], messages: [...]}.

`archived` is the tab-close semantic: closing a chat tab flips it true;
the Chat Archive tab lists archived chats and re-opening one flips it
back. On library open, chats with archived=false are the tabs to restore
- a quit with chats open resumes where the user left off.

Message shapes mirror the wire (OpenAI-style role/content) plus UI-only
fields the loop emits (thoughts, tool cards); chat.py owns those.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
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


def new_chat(root: Path, model: str = "", provider: str = "") -> dict:
    chat = {
        "id": uuid.uuid4().hex[:12],
        "title": "New chat",
        "createdTs": int(time.time() * 1000),
        "updatedTs": int(time.time() * 1000),
        "archived": False,
        "provider": provider,   # config provider name; "" = the default
        "model": model,         # model id as the provider's API lists it
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
    # pid AND thread: the bridge thread and a chat worker can save the
    # same doc concurrently - they must never share a tmp file
    tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
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
                    "provider": c.get("provider") or "",
                    "updatedTs": c.get("updatedTs") or 0,
                    "messages": len(c.get("messages") or [])})
    out.sort(key=lambda c: -(c.get("updatedTs") or 0))
    return out


def purge_empty_archived(root: Path) -> int:
    """Delete archived chats with no messages - dead weight from tabs that
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


# --------------------------------------------------------------------------
# forking - a NEW chat whose history is another chat truncated at a point

# Injected as a THOUGHT (assistant `thinking`) so it rides the wire the
# way the model's own reasoning does. First person on purpose.
FORK_NOTE = (
    "Note: this conversation was forked from another chat at this point. "
    "Tool calls ran after the fork point in the original conversation, so "
    "files or other external state may have changed since this history "
    "was recorded. Before making any changes, I should re-check the "
    "current state instead of trusting earlier observations.")


def fork_messages(messages: list, idx: int) -> tuple[list[dict], bool]:
    """A deep-copied prefix [0..idx] with tool-call pairing repaired,
    plus whether tool calls happened at-or-after the fork point (the
    dropped tail ran tools, or calls at the boundary lost their results).

    Pairing rule: an assistant's `tool_calls` and its `role:"tool"`
    results must never be split - strict chat templates reject either
    half alone. Calls whose results fell past the cut are trimmed from
    the copied assistant message (the fork simply never made them)."""
    msgs = messages or []
    if not msgs:
        return [], False
    idx = max(0, min(int(idx), len(msgs) - 1))
    sliced = copy.deepcopy(msgs[: idx + 1])
    tools_after = any(
        m.get("role") == "tool"
        or (m.get("role") == "assistant" and m.get("tool_calls"))
        for m in msgs[idx + 1:])
    have = {m.get("tool_call_id") for m in sliced if m.get("role") == "tool"}
    for m in sliced:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            kept = [c for c in m["tool_calls"] if c.get("id") in have]
            if len(kept) != len(m["tool_calls"]):
                tools_after = True   # calls ran; their results are gone
                if kept:
                    m["tool_calls"] = kept
                else:
                    del m["tool_calls"]
    return sliced, tools_after


def fork_chat(root: Path, chat_id: str, idx: int) -> tuple[dict, bool]:
    """Fork `chat_id` after message `idx` into a brand-new chat: same
    setup (provider/model/permissions/network/container/env/folders),
    truncated history, artifacts referenced by the kept history copied
    over. If tools ran after the fork point, FORK_NOTE lands as a thought
    on the LAST assistant message - the one spot thought truncation
    always keeps on the wire. Returns (chat, note_injected)."""
    src = load_chat(root, chat_id)
    msgs, tools_after = fork_messages(src.get("messages") or [], idx)
    now = int(time.time() * 1000)
    title = str(src.get("title") or "").strip()
    c = {
        "id": uuid.uuid4().hex[:12],
        "title": (title + " (fork)")[:80]
                 if title and title != "New chat" else "New chat",
        "createdTs": now,
        "updatedTs": now,
        "archived": False,
        "provider": src.get("provider") or "",
        "model": src.get("model") or "",
        "folders": copy.deepcopy(src.get("folders") or []),
        "images": [],
        "messages": msgs,
        "forkedFrom": {"chat": src.get("id"), "index": int(idx)},
    }
    for k in ("permMode", "network", "container", "env", "timeTravelMs",
              "artifactsOff", "timeSignalsOff", "knowledgeOff",
              "mcpPerms", "envHidden"):
        if src.get(k):
            c[k] = copy.deepcopy(src[k])
    if "thoughtTruncation" in src:   # False is a real value here
        c["thoughtTruncation"] = bool(src["thoughtTruncation"])
    if tools_after:
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                prior = str(m.get("thinking") or "").rstrip()
                m["thinking"] = (prior + "\n\n" if prior else "") + FORK_NOTE
                break
        else:
            # no assistant to carry the thought (forked at the very first
            # message): a thinking-only assistant entry carries it instead
            msgs.append({"role": "assistant", "content": "",
                         "thinking": FORK_NOTE, "ts": now})
    # artifacts delivered within the kept history: copy the files so the
    # fork's chips keep opening even if the source chat is deleted later
    names = {str(it.get("name")) for m in msgs if m.get("role") == "artifact"
             for it in (m.get("items") or []) if it.get("name")}
    names = {n for n in names if n not in ("", ".", "..")
             and "/" not in n and "\\" not in n}
    if names:
        src_dir = artifacts_dir(root, str(src.get("id")))
        dst_dir = artifacts_dir(root, c["id"], create=True)
        for n in sorted(names):
            try:
                if (src_dir / n).is_file():
                    shutil.copy2(src_dir / n, dst_dir / n)
            except OSError:
                pass   # a missing artifact only degrades its chip
        arts = [a for a in (src.get("artifacts") or [])
                if str(a.get("name")) in names]
        if arts:
            c["artifacts"] = copy.deepcopy(arts)
        dis = [n for n in (src.get("artifactsDismissed") or []) if n in names]
        if dis:
            c["artifactsDismissed"] = dis
    save_chat(root, c)
    return c, tools_after


def delete_message(root: Path, chat_id: str, idx: int,
                   part: str = "message") -> int:
    """Remove ONE message from the history - plus whatever must go with
    it to keep the wire valid: an assistant's tool results follow their
    calling turn out; a deleted tool result takes its entry out of the
    parent's `tool_calls` (and takes the parent too when nothing of that
    turn remains). part="thinking" removes just the THOUGHT from an
    assistant message, and the whole message only when nothing else
    remains of that turn. Returns how many messages were removed."""
    c = load_chat(root, chat_id)
    msgs = c.get("messages") or []
    idx = int(idx)
    if not (0 <= idx < len(msgs)):
        raise ChatError("that message no longer exists - reload the chat")
    m = msgs[idx]
    if part == "thinking":
        if m.get("role") != "assistant" or not m.get("thinking"):
            raise ChatError("that entry has no thought to delete")
        m.pop("thinking", None)
        if m.get("content") or m.get("tool_calls"):
            save_chat(root, c)
            return 0
        # a thinking-only turn: nothing remains - drop the message
        c["messages"] = msgs[:idx] + msgs[idx + 1:]
        save_chat(root, c)
        return 1
    drop = {idx}
    if m.get("role") == "assistant" and m.get("tool_calls"):
        ids = {t.get("id") for t in m["tool_calls"]}
        for j in range(idx + 1, len(msgs)):
            if msgs[j].get("role") == "tool" \
                    and msgs[j].get("tool_call_id") in ids:
                drop.add(j)
    elif m.get("role") == "tool":
        cid = m.get("tool_call_id")
        for j in range(idx - 1, -1, -1):
            pm = msgs[j]
            if pm.get("role") == "assistant" and pm.get("tool_calls"):
                kept = [t for t in pm["tool_calls"] if t.get("id") != cid]
                if kept:
                    pm["tool_calls"] = kept
                else:
                    del pm["tool_calls"]
                    if not (pm.get("content") or pm.get("thinking")):
                        drop.add(j)   # nothing left of that turn at all
                break
            if pm.get("role") in ("assistant", "user"):
                break   # no owning turn found - just drop the result
    c["messages"] = [x for j, x in enumerate(msgs) if j not in drop]
    save_chat(root, c)
    return len(drop)


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
