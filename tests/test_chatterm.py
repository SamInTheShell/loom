"""Chat-mirror terminal tests: the setup is computed from the chat
document exactly as the shell tool sees it, the sync restarts only a
LIVE session whose setup actually changed, and the chat setters trigger
that sync. Script-style: run with
`uv run python tests/test_chatterm.py`; nonzero exit on failure."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-cterm-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import chats, chatterm, containers, terminals  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


from loom.app import Bus, JsApi  # noqa: E402

api = JsApi(Bus())
with tempfile.TemporaryDirectory(prefix="loomtest-ctermlib-") as d:
    r = api.library_create(d + "/lib")
    check("library created", r["ok"], str(r))
    rt = api._need_root()
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n"
        "containers:\n  default: sandbox\n")

    # ---------- setup_for mirrors the chat's container view ----------
    c = chats.new_chat(rt)
    c["container"] = ""
    c["folders"] = [{"path": d, "mode": "write"},
                    {"path": d + "/lib", "mode": "view"}]
    c["network"] = "loopback"
    c["env"] = "staging"
    chats.save_chat(rt, c)
    s = chatterm.setup_for(rt, chats.load_chat(rt, c["id"]))
    check("folders ride through with their modes",
          s["folders"] == [{"path": d, "mode": "write"},
                           {"path": d + "/lib", "mode": "view"}], str(s))
    check("network is the canonical mode", s["network"] == "loopback")
    check("the environment name rides", s["env"] == "staging")
    check("knowledge mounts by default",
          s["knowledge"] == str(rt / "knowledge"), str(s["knowledge"]))
    check("uploads rides the setup (read-only user files)",
          s["uploads"] == str(chats.artifacts_dir(rt, c["id"]) / "uploads"),
          str(s["uploads"]))
    check("home is the CHAT's home - the model's /home/loom",
          s["home"] == str(containers.chat_home(c["id"])), str(s["home"]))

    c["knowledgeOff"] = True
    chats.save_chat(rt, c)
    s2 = chatterm.setup_for(rt, chats.load_chat(rt, c["id"]))
    check("the knowledge cut drops the mount", s2["knowledge"] is None)

    # legacy boolean network values canonicalize like the shell tool's
    c["network"] = True
    chats.save_chat(rt, c)
    check("legacy network booleans canonicalize",
          chatterm.setup_for(rt, chats.load_chat(rt, c["id"]))["network"]
          == containers.net_mode(True))

    # ---------- the extra chat-view mounts ----------
    kdir = rt / "knowledge"
    kdir.mkdir(exist_ok=True)
    updir = Path(d) / "ups"
    updir.mkdir(exist_ok=True)
    vol = terminals._extra_mounts(kdir, updir)
    check("knowledge mounts read-only at /knowledge",
          f"{kdir.resolve()}:/knowledge:ro" in vol, str(vol))
    check("uploads mounts READ-ONLY at /uploads",
          f"{updir.resolve()}:/uploads:ro" in vol, str(vol))
    check("no /artifacts mount exists anywhere",
          all("/artifacts" not in v for v in vol), str(vol))
    check("cut mounts vanish", terminals._extra_mounts(None, None) == [])
    check("a missing uploads dir is skipped",
          terminals._extra_mounts(None, Path(d) / "nope") == [])

    # ---------- sync: restart only a live session that drifted ----------
    sid = chatterm.sid_for(c["id"])
    check("the session id is chat-scoped", sid == "chat-" + c["id"])

    opened = []
    _real_open = chatterm._open
    chatterm._open = lambda push, root, cid, setup, cols, rows: \
        opened.append((cid, setup, cols, rows))
    _real_alive = terminals.alive
    push = lambda ev: None

    terminals.alive = lambda s: False
    check("a dead session never restarts",
          chatterm.sync(push, rt, c["id"]) is False and not opened)

    terminals.alive = lambda s: True
    want = chatterm.setup_for(rt, chats.load_chat(rt, c["id"]))
    with chatterm._LOCK:
        chatterm._SETUP[sid] = dict(want)
        chatterm._DIMS[sid] = (100, 40)
    check("a matching setup stays put",
          chatterm.sync(push, rt, c["id"]) is False and not opened)

    c = chats.load_chat(rt, c["id"])
    c["network"] = "none"
    chats.save_chat(rt, c)
    check("a drifted setup restarts with the chat's dims",
          chatterm.sync(push, rt, c["id"]) is True
          and opened and opened[0][0] == c["id"]
          and opened[0][1]["network"] == "none"
          and opened[0][2:] == (100, 40), str(opened))
    chatterm._open = _real_open
    terminals.alive = _real_alive
    with chatterm._LOCK:
        chatterm._SETUP.pop(sid, None)
        chatterm._DIMS.pop(sid, None)

    # ---------- the setters trigger the sync ----------
    synced = []
    _real_async = chatterm.sync_async
    chatterm.sync_async = lambda push, root, cid: synced.append(str(cid))
    api.chat_set_network(c["id"], "on")
    api.chat_set_folders(c["id"], [])
    api.chat_set_container(c["id"], "")
    api.chat_set_env(c["id"], "")
    api.chat_set_knowledge(c["id"], True)
    check("every container-view setter syncs the mirror",
          synced == [c["id"]] * 5, str(synced))
    api.chat_set_thought_truncation(c["id"], True)
    api.chat_set_time_travel(c["id"], 1000)
    api.chat_set_artifacts(c["id"], True)   # delivery is not a mount
    check("non-container settings never restart the shell",
          len(synced) == 5, str(synced))
    chatterm.sync_async = _real_async

    # ---------- the child window's header info ----------
    r = api.chat_term_info(c["id"])
    check("info resolves the default container",
          r["ok"] and r["data"]["container"] == "sandbox", str(r))
    check("info carries the canonical view",
          r["data"]["network"] == "on" and r["data"]["env"] == ""
          and r["data"]["knowledge"] is True
          and r["data"]["uploads"] is False
          and r["data"]["folders"] == []
          and r["data"]["sid"] == sid, str(r))
    r = api.chat_term_open("no-such-chat", 80, 24)
    check("opening a missing chat fails before the worker thread",
          not r["ok"], str(r))

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
