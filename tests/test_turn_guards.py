"""Incomplete-tool-call guards: unparsed tool-call markup detection and
the truncated-arguments refusal (a half-arrived call must never execute,
and the loop must stay alive so the model retries). Script-style: run
with `uv run python tests/test_turn_guards.py`; nonzero exit on
failure."""

import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-guards-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import chats  # noqa: E402
from loom import chat as chatmod  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- failed-call detection ----------
detect = chatmod._detect_failed_call

cut_off = ("Now the full frontend:\n\n<tool_call>\n<function=write_file>\n"
           "<parameter=content>\n<!doctype html>... (stream died here)")
got = detect(cut_off)
check("a cut-off tool call is a FAILED call, name guessed",
      got is not None and got[1] == "write_file", str(got))
check("the prose before the call survives, the markup is discarded",
      got[0] == "Now the full frontend:", str(got))
whole = ('<tool_call>\n{"name": "shell", "arguments": {"command": "ls"}}'
         "\n</tool_call>")
got = detect(whole)
check("a complete call flushed as the WHOLE reply is a failed call",
      got is not None and got[1] == "shell" and got[0] == "", str(got))
quoted = ("Templates emit blocks like\n```\n<tool_call>\n"
          "<function=run>x</function>\n</tool_call>\n```\nwhich…")
check("markup quoted in a code fence is prose, not a failed call",
      detect(quoted) is None)
prose_and_block = ("Here is a long explanation of the format that goes "
                   "on for a while.\n<tool_call>ok</tool_call>\nmore "
                   "prose after it, clearly an answer")
check("balanced markup inside a real answer is not a failed call",
      detect(prose_and_block) is None)
check("plain prose is never a failed call",
      detect("just an answer about <b>") is None)
check("mistral-style markers detect",
      detect("[TOOL_CALLS] {\"name\": \"grep\", ") is not None
      and detect('[TOOL_CALLS] {"name": "grep", ')[1] == "grep")
check("an html <function> mention alone is not markup",
      detect("the <function> element of html") is None)
check("unknown call shapes still guess a name",
      detect("<tool_call>\ngarbage")[1] == "unknown_tool")

# ---------- truncated arguments never execute ----------
from loom.app import Bus, JsApi  # noqa: E402

api = JsApi(Bus())
with tempfile.TemporaryDirectory(prefix="loomtest-guardlib-") as d:
    r = api.library_create(d + "/lib")
    check("library created", r["ok"], str(r))
    rt = api._need_root()
    c = chats.new_chat(rt)
    chats.save_chat(rt, c)

    events = []
    ev = lambda kind, **kw: events.append((kind, kw))
    executed = []
    _orig_exec = chatmod._exec_tool
    chatmod._exec_tool = lambda *a, **kw: executed.append(a) or "ran"

    call = {"id": "c1", "name": "write_file",
            "args": '{"path": "/artifacts/index.html", "content": "<!doc'}
    chatmod._run_tool(rt, {}, c, call, threading.Event(), ev)
    chatmod._exec_tool = _orig_exec

    check("the tool never executed", executed == [], str(executed))
    tool_msg = c["messages"][-1]
    check("a failed tool RESULT went back to the model",
          tool_msg["role"] == "tool" and tool_msg["ok"] is False
          and "truncated" in tool_msg["content"], str(tool_msg))
    kinds = [k for k, _ in events]
    check("the call surfaced to the UI, then failed - no permission gate",
          kinds == ["tool_call", "tool_result"], str(kinds))
    res_ev = dict(events[1][1])
    check("the result event carries the failure",
          res_ev["ok"] is False and "truncated" in res_ev["result"])
    args_ev = events[0][1]["args"]
    check("the raw truncated args are shown, not executed",
          "_raw" in args_ev and args_ev["_raw"].startswith('{"path"'))

    # intact args still flow through to execution
    events.clear()
    executed.clear()
    chatmod._exec_tool = lambda *a, **kw: executed.append(a) or "ok!"
    good = {"id": "c2", "name": "knowledge_search",
            "args": '{"query": "x"}'}
    # permission: always-ask default would gate - use a mode-free cfg and
    # answer the gate immediately via a pre-set decision? Simpler: deny
    # path also proves parsing worked (no bad-args refusal fired)
    chatmod._run_tool(rt, {"permissionModes":
                           {"always-ask": {"knowledge_search": "allow"}},
                           "chat": {"permission_mode": "always-ask"}},
                      c, good, threading.Event(), ev)
    chatmod._exec_tool = _orig_exec
    check("intact args still execute", len(executed) == 1
          and c["messages"][-1]["ok"] is True, str(c["messages"][-1]))

    # a truncated-by-limit call names the limit in its refusal
    events.clear()
    chatmod._exec_tool = lambda *a, **kw: "never"
    cut = {"id": "c3", "name": "write_file", "cut": True, "budget": 32768,
           "args": '{"path": "/x", "content": "<!doc'}
    chatmod._run_tool(rt, {}, c, cut, threading.Event(), ev)
    chatmod._exec_tool = _orig_exec
    check("the refusal names the output limit that caused the cut",
          "max_tokens 32768" in c["messages"][-1]["content"],
          c["messages"][-1]["content"])

    # ---------- the configurable read gate ----------
    big = rt / "big.txt"
    big.write_text("line\n" * 5000)   # ~25k chars
    ch2 = chats.new_chat(rt)
    ch2["folders"] = [{"path": str(rt), "mode": "view"}]
    chats.save_chat(rt, ch2)
    mnt = "/mnt/" + rt.name + "/big.txt"
    cfg_small = {"chat": {"read_gate": 1000}}    # 1k tokens = 4k chars
    try:
        chatmod._exec_tool(rt, cfg_small, ch2, "read_file",
                           {"path": mnt}, threading.Event())
        check("a whole read past the gate is refused", False)
    except chats.ChatError as e:
        check("a whole read past the gate is refused",
              "read gate" in str(e) and "1000 tokens" in str(e), str(e))
        check("the refusal teaches slicing", "offset" in str(e))
    sl = chatmod._exec_tool(rt, cfg_small, ch2, "read_file",
                            {"path": mnt, "offset": 10, "limit": 5},
                            threading.Event())
    check("slices still work under the gate",
          sl.startswith("[lines 10-14 of 5001]"), sl[:40])
    whole_txt = chatmod._exec_tool(rt, {"chat": {"read_gate": 0}}, ch2,
                                   "read_file", {"path": mnt},
                                   threading.Event())
    check("read_gate 0 disables the gate", whole_txt.count("\n") == 5000)
    whole_txt = chatmod._exec_tool(rt, {"chat": {}}, ch2, "read_file",
                                   {"path": mnt}, threading.Event())
    check("the default gate (32k tokens) admits a 25k-char file",
          whole_txt.count("\n") == 5000)

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
