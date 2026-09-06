"""Chat fork tests: prefix slicing with tool-call pairing repair, the
state-check thought injection (and its survival on the wire under thought
truncation), setup + artifact carry-over. Script-style: run with
`uv run python tests/test_fork.py`; nonzero exit on failure."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-fork-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import chats, libconfig  # noqa: E402
from loom.chats import FORK_NOTE, fork_messages  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def call(cid, name="shell"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


# indexes:      0            1                 2      3      4        5    6           7      8
MSGS = [
    {"role": "user", "content": "u0", "ts": 1},                       # 0
    {"role": "assistant", "content": "a1", "ts": 2,                    # 1
     "tool_calls": [call("c1"), call("c2")]},
    {"role": "tool", "tool_call_id": "c1", "name": "shell",           # 2
     "content": "r1", "ok": True, "ts": 3},
    {"role": "tool", "tool_call_id": "c2", "name": "shell",           # 3
     "content": "r2", "ok": True, "ts": 4},
    {"role": "assistant", "content": "a4", "thinking": "hm", "ts": 5},  # 4
    {"role": "user", "content": "u5", "ts": 6},                        # 5
    {"role": "assistant", "content": "a6", "ts": 7,                    # 6
     "tool_calls": [call("c3")]},
    {"role": "tool", "tool_call_id": "c3", "name": "shell",           # 7
     "content": "r3", "ok": True, "ts": 8},
    {"role": "assistant", "content": "a8", "ts": 9},                   # 8
]

# ---------- fork_messages: slicing + pairing repair ----------
s, after = fork_messages(MSGS, 8)
check("fork at the end keeps everything, no note needed",
      len(s) == 9 and after is False
      and s[1]["tool_calls"] and len(s[1]["tool_calls"]) == 2, str(after))

s, after = fork_messages(MSGS, 4)
check("fork at a clean assistant keeps intact pairs",
      len(s) == 5 and len(s[1]["tool_calls"]) == 2 and after is True)

s, after = fork_messages(MSGS, 5)
check("fork at a user message flags the tail's tool calls",
      len(s) == 6 and after is True)

s, after = fork_messages(MSGS, 1)
check("fork at an assistant strips calls whose results fell past the cut",
      len(s) == 2 and "tool_calls" not in s[1] and after is True, str(s[1]))

s, after = fork_messages(MSGS, 2)
check("fork at a mid tool result trims the parent to the kept calls",
      len(s) == 3 and [c["id"] for c in s[1]["tool_calls"]] == ["c1"]
      and after is True, str(s[1]))

s, after = fork_messages(MSGS, 3)
check("fork at the last sibling result keeps every call",
      len(s) == 4 and len(s[1]["tool_calls"]) == 2 and after is True)

s, after = fork_messages(MSGS, 0)
check("fork at the very first message",
      len(s) == 1 and s[0]["role"] == "user" and after is True)

check("fork_messages clamps a wild index",
      len(fork_messages(MSGS, 999)[0]) == 9
      and len(fork_messages(MSGS, -5)[0]) == 1)
check("deep copy - the source is untouched",
      len(MSGS[1]["tool_calls"]) == 2 and "forked" not in str(MSGS[0]))
check("empty history forks to empty", fork_messages([], 0) == ([], False))

# ---------- fork_chat: the whole document ----------
from loom.app import Bus, JsApi  # noqa: E402

api = JsApi(Bus())
with tempfile.TemporaryDirectory(prefix="loomtest-forklib-") as d:
    r = api.library_create(d + "/lib")
    check("library created", r["ok"], str(r))
    rt = api._need_root()
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n")

    src = chats.new_chat(rt, model="qwen3", provider="ws")
    src["title"] = "Refactor plan"
    src["permMode"] = "allow-edits"
    src["network"] = "full"
    src["container"] = "dev"
    src["env"] = "staging"
    src["folders"] = [{"path": "/tmp/x", "mode": "write"}]
    src["messages"] = [dict(m) for m in MSGS]
    chats.save_chat(rt, src)

    # fork mid-history, tools in the tail → note on the LAST assistant
    r = api.chat_fork(src["id"], 4)
    check("chat_fork answers with the new chat",
          r["ok"] and r["data"]["toolNote"] is True
          and r["data"]["chat"]["totalMessages"] == 5, str(r))
    c = r["data"]["chat"]
    check("fork gets its own id and a fork title",
          c["id"] != src["id"] and c["title"] == "Refactor plan (fork)")
    check("fork records its origin",
          c.get("forkedFrom") == {"chat": src["id"], "index": 4}, str(c.get("forkedFrom")))
    check("setup carries over",
          c["provider"] == "ws" and c["model"] == "qwen3"
          and c["permMode"] == "allow-edits" and c["network"] == "full"
          and c["container"] == "dev" and c["env"] == "staging"
          and c["folders"] == [{"path": "/tmp/x", "mode": "write"}])
    last = c["messages"][-1]
    check("the note is APPENDED to the last assistant's thinking",
          last["role"] == "assistant"
          and last["thinking"].startswith("hm")
          and last["thinking"].endswith(FORK_NOTE), last["thinking"][:80])
    check("the fork is persisted and loadable",
          chats.load_chat(rt, c["id"])["messages"] == c["messages"])
    check("the source chat is untouched",
          len(chats.load_chat(rt, src["id"])["messages"]) == 9)

    # the note must actually reach the model: last assistant's thinking
    # survives thought_truncation (the default)
    from loom import chat as chatmod
    cfg = libconfig.load(rt)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, c["id"]))
    asst = [w for w in wire if w["role"] == "assistant"]
    check("the note rides the wire despite thought truncation",
          asst and FORK_NOTE in asst[-1]["content"]
          and "<think>" in asst[-1]["content"], str(asst[-1])[:200])
    earlier = [w for w in asst[:-1] if "<think>" in w.get("content", "")]
    check("earlier thoughts still truncate as before", not earlier)

    # fork at the end → no note anywhere
    r = api.chat_fork(src["id"], 8)
    c2 = r["data"]["chat"]
    check("no tools after the cut → no note",
          r["data"]["toolNote"] is False
          and all(FORK_NOTE not in str(m.get("thinking") or "")
                  for m in c2["messages"]))

    # fork at the very first message → synthetic thinking-only assistant
    r = api.chat_fork(src["id"], 0)
    c3 = r["data"]["chat"]
    check("no assistant in the slice → a thinking-only carrier is added",
          r["ok"] and len(c3["messages"]) == 2
          and c3["messages"][1]["role"] == "assistant"
          and c3["messages"][1]["content"] == ""
          and c3["messages"][1]["thinking"] == FORK_NOTE, str(c3["messages"]))

    # artifacts referenced by the kept history come along
    src2 = chats.new_chat(rt)
    adir = chats.artifacts_dir(rt, src2["id"], create=True)
    (adir / "notes.md").write_text("kept")
    (adir / "later.md").write_text("dropped")
    src2["artifacts"] = [{"name": "notes.md", "bytes": 4},
                         {"name": "later.md", "bytes": 7}]
    src2["artifactsDismissed"] = ["notes.md"]
    src2["messages"] = [
        {"role": "user", "content": "u", "ts": 1},
        {"role": "assistant", "content": "a", "ts": 2},
        {"role": "artifact", "items": [{"name": "notes.md", "bytes": 4}], "ts": 3},
        {"role": "artifact", "items": [{"name": "later.md", "bytes": 7}], "ts": 4},
    ]
    chats.save_chat(rt, src2)
    r = api.chat_fork(src2["id"], 2)
    c4 = r["data"]["chat"]
    ndir = chats.artifacts_dir(rt, c4["id"])
    check("kept artifact file is copied, dropped one is not",
          (ndir / "notes.md").is_file() and not (ndir / "later.md").exists(),
          str(list(ndir.glob("*")) if ndir.exists() else "no dir"))
    check("artifact records + dismissals filter to the kept names",
          c4.get("artifacts") == [{"name": "notes.md", "bytes": 4}]
          and c4.get("artifactsDismissed") == ["notes.md"], str(c4.get("artifacts")))
    check("no tool calls in that tail → no note",
          r["data"]["toolNote"] is False)

    # a fresh untitled source keeps "New chat" so autotitle still fires
    src3 = chats.new_chat(rt)
    src3["messages"] = [{"role": "user", "content": "hi", "ts": 1}]
    chats.save_chat(rt, src3)
    r = api.chat_fork(src3["id"], 0)
    check("an untitled source forks as New chat (autotitle keeps working)",
          r["ok"] and r["data"]["chat"]["title"] == "New chat", str(r))

# ---------- delete_message: single-entry removal with pairing cascade ----------
with tempfile.TemporaryDirectory(prefix="loomtest-dellib-") as d:
    r = api.library_create(d + "/lib")
    check("delete: library created", r["ok"], str(r))
    rt = api._need_root()

    def fresh():
        c = chats.new_chat(rt)
        c["messages"] = [dict(m, tool_calls=[dict(t) for t in m["tool_calls"]])
                         if "tool_calls" in m else dict(m) for m in MSGS]
        chats.save_chat(rt, c)
        return c["id"]

    cid = fresh()
    r = api.chat_delete_message(cid, 5)
    got = chats.load_chat(rt, cid)["messages"]
    check("deleting a user message removes just it",
          r["ok"] and r["data"]["deleted"] == 1 and len(got) == 8
          and [m.get("content") for m in got if m["role"] == "user"] == ["u0"],
          str(r))

    cid = fresh()
    r = api.chat_delete_message(cid, 1)   # assistant with calls c1+c2
    got = chats.load_chat(rt, cid)["messages"]
    check("deleting a calling turn takes its tool results along",
          r["ok"] and r["data"]["deleted"] == 3 and len(got) == 6
          and not any(m["role"] == "tool"
                      and m["tool_call_id"] in ("c1", "c2") for m in got),
          str(got))

    cid = fresh()
    r = api.chat_delete_message(cid, 2)   # result of c1 (parent keeps c2)
    got = chats.load_chat(rt, cid)["messages"]
    check("deleting one result trims the parent's call list",
          r["ok"] and r["data"]["deleted"] == 1
          and [t["id"] for t in got[1]["tool_calls"]] == ["c2"], str(got[1]))

    cid = fresh()
    api.chat_delete_message(cid, 2)       # drop c1's result...
    r = api.chat_delete_message(cid, 2)   # ...then c2's (indices shifted)
    got = chats.load_chat(rt, cid)["messages"]
    check("the parent keeps its text when its last call goes",
          r["ok"] and got[1]["role"] == "assistant"
          and got[1]["content"] == "a1" and "tool_calls" not in got[1],
          str(got[1]))

    cid = fresh()
    api.chat_delete_message(cid, 6)       # a6 (calls c3) leaves with r3
    got = chats.load_chat(rt, cid)["messages"]
    check("wire pairing stays intact after cascades",
          all(t["tool_call_id"] in {c["id"] for a in got
                                    if a["role"] == "assistant"
                                    for c in a.get("tool_calls") or []}
              for t in got if t["role"] == "tool"), str(got))

    # a call-only turn evaporates once its last result is deleted
    c = chats.new_chat(rt)
    c["messages"] = [
        {"role": "user", "content": "u", "ts": 1},
        {"role": "assistant", "content": "", "ts": 2, "tool_calls": [call("x1")]},
        {"role": "tool", "tool_call_id": "x1", "name": "shell",
         "content": "r", "ok": True, "ts": 3},
    ]
    chats.save_chat(rt, c)
    r = api.chat_delete_message(c["id"], 2)
    got = chats.load_chat(rt, c["id"])["messages"]
    check("a bare calling turn goes with its only result",
          r["ok"] and r["data"]["deleted"] == 2
          and [m["role"] for m in got] == ["user"], str(got))

    # thought deletion: the thinking goes, the turn stays - unless the
    # thought was all there was
    cid = fresh()
    r = api.chat_delete_message(cid, 4, "thinking")   # a4 carries "hm"
    got = chats.load_chat(rt, cid)["messages"]
    check("deleting a thought keeps the reply",
          r["ok"] and r["data"]["deleted"] == 0
          and got[4]["content"] == "a4" and "thinking" not in got[4],
          str(got[4]))
    r = api.chat_delete_message(cid, 4, "thinking")
    check("a turn without a thought refuses",
          not r["ok"] and "no thought" in r["error"], str(r))

    c = chats.new_chat(rt)
    c["messages"] = [
        {"role": "user", "content": "u", "ts": 1},
        {"role": "assistant", "content": "",
         "thinking": "only a thought", "ts": 2},
    ]
    chats.save_chat(rt, c)
    r = api.chat_delete_message(c["id"], 1, "thinking")
    got = chats.load_chat(rt, c["id"])["messages"]
    check("a thinking-only turn vanishes with its thought",
          r["ok"] and r["data"]["deleted"] == 1
          and [m["role"] for m in got] == ["user"], str(got))

    r = api.chat_delete_message(cid, 99)
    check("an out-of-range delete fails cleanly",
          not r["ok"] and "no longer exists" in r["error"], str(r))

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
