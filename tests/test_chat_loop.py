"""End-to-end chat loop test against a FAKE llama-server speaking SSE over
a unix socket — exercises streaming deltas, a tool call (knowledge_search,
allowed by default), the second turn, stats injection, persistence, and
the ask-gate deny path. No GUI, no real model.

Run: uv run python tests/test_chat_loop.py
"""

import json
import os
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import chat, chats, libconfig, library, srv  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}" + (f" — {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- the fake llama-server ----------
class FakeLlama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "unix"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n))
        self.server.requests.append(req)
        if self.server.mode == "stall":
            # a busy server chewing a huge prompt: NO response headers for
            # a long time — cancels must not have to wait this out
            time.sleep(30)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def sse(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        if self.server.mode == "error":
            # llama-server's in-stream failure shape: an SSE error chunk
            sse({"error": {"code": 500, "message": "kaboom from fake server"}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        has_tool_result = any(m.get("role") == "tool" for m in req["messages"])
        if not has_tool_result and self.server.mode == "tools":
            sse({"choices": [{"delta": {"reasoning_content": "hmm "}}]})
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": self.server.tool_name, "arguments": ""}}]}}]})
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": json.dumps(self.server.tool_args)}}]}}]})
        else:
            for piece in ("Hello ", "from ", "fake llama"):
                sse({"choices": [{"delta": {"content": piece}}]})
        sse({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                      "total_tokens": 10},
             "timings": {"predicted_per_second": 42.5, "predicted_n": 3}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def start_fake(sock_path, mode, tool_name="knowledge_search", tool_args=None):
    server = UnixHTTPServer(sock_path, FakeLlama)
    server.mode = mode
    server.tool_name = tool_name
    server.tool_args = tool_args or {"query": "needle"}
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_chat(root, model_name, sock, mode, tool_name="knowledge_search",
             tool_args=None, answer=None, folders=None, shutdown_after=True):
    server = start_fake(sock, mode, tool_name, tool_args)
    mid = libconfig.load(root)["models"][0]["id"]
    with srv._lock:
        srv._servers[mid] = {"id": mid, "state": "running", "sock": sock}
    c = chats.new_chat(root, model_name)
    if folders:
        c["folders"] = folders
    c["messages"].append({"role": "user", "content": "find the needle"})
    return _drive(root, c, server, mode, answer, shutdown_after)


def _drive(root, c, server, mode, answer, shutdown_after=True):
    chats.save_chat(root, c)
    events = []
    done = threading.Event()

    def push(ev):
        events.append(ev)
        if ev["kind"] == "tool_wait" and answer:
            threading.Thread(target=lambda: (time.sleep(0.1),
                             chat.GATE.answer(ev["callId"], answer)),
                             daemon=True).start()
        if ev["kind"] in ("done", "error"):
            done.set()

    chat.send(root, c["id"], push)
    ok = done.wait(30)
    if shutdown_after:
        server.shutdown()
    return c["id"], events, server.requests, ok


with tempfile.TemporaryDirectory() as d:
    root = library.create_library(d + "/lib")
    (root / "knowledge" / "notes.md").write_text("the needle is here")
    (root / "loom.yaml").write_text(
        "models:\n- name: fake\n  model: /x.gguf\n  context: 4096\n"
        "permissions:\n  mode: ask\n  tools:\n    knowledge_search: allow\n"
        "    shell: ask\n")

    sock1 = os.environ["LOOM_HOME"] + "/fake1.sock"
    cid, events, reqs, finished = run_chat(root, "fake", sock1, mode="tools")
    kinds = [e["kind"] for e in events]
    check("loop finished", finished, str(kinds))
    check("think delta arrived", "think" in kinds)
    check("tool_call fired", "tool_call" in kinds)
    check("tool executed without asking (allow)", "tool_wait" not in kinds)
    tr = next((e for e in events if e["kind"] == "tool_result"), {})
    check("knowledge_search found the file", "notes.md" in str(tr.get("result", "")), str(tr))
    check("final content streamed", any(e["kind"] == "delta" and "fake llama" in e.get("text", "") or e.get("text") == "Hello " for e in events))
    check("stats pushed", any(e["kind"] == "stats" and (e.get("usage") or {}).get("total_tokens") == 10 for e in events))
    check("done event", kinds[-1] == "done")

    saved = chats.load_chat(root, cid)
    roles = [m["role"] for m in saved["messages"]]
    check("messages persisted (user, asst+tools, tool, asst)",
          roles == ["user", "assistant", "tool", "assistant"], str(roles))
    check("timings persisted on assistant msg",
          any(m.get("timings") for m in saved["messages"] if m["role"] == "assistant"))

    def wait_title(chat_id, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            t = chats.load_chat(root, chat_id).get("title")
            if t and t != "New chat":
                return t
            time.sleep(0.2)
        return chats.load_chat(root, chat_id).get("title")

    # titling is async after done; the fake server for this run is gone, so
    # the model path fails and the first-words fallback must land
    check("auto-title fallback set", wait_title(cid).startswith("find the needle"),
          wait_title(cid, 1))
    check("stream stats injected into request",
          reqs and reqs[0].get("timings_per_token") is True
          and (reqs[0].get("stream_options") or {}).get("include_usage") is True)
    check("tools offered to the model",
          any(t["function"]["name"] == "knowledge_search" for t in reqs[0].get("tools", [])))
    check("write_file offered without write folders (/artifacts is rw)",
          any(t["function"]["name"] == "write_file" for t in reqs[0].get("tools", [])))

    # ---------- ask-gate: user denies a shell call (write folder attached,
    # so shell is available and the gate genuinely asks) ----------
    workdir = tempfile.mkdtemp(prefix="loomtest-work-")
    sock2 = os.environ["LOOM_HOME"] + "/fake2.sock"
    cid2, events2, reqs2, finished2 = run_chat(
        root, "fake", sock2, mode="tools", tool_name="shell",
        tool_args={"command": "echo hi"}, answer="deny",
        folders=[{"path": workdir, "mode": "write"}])
    kinds2 = [e["kind"] for e in events2]
    check("deny: loop finished", finished2, str(kinds2))
    check("deny: gate asked", "tool_wait" in kinds2)
    tr2 = next((e for e in events2 if e["kind"] == "tool_result"), {})
    check("deny: tool refused", tr2.get("ok") is False and "declined" in str(tr2.get("result")), str(tr2))
    check("deny: shell was offered (write folder)",
          any(t["function"]["name"] == "shell" for t in reqs2[0].get("tools", [])))

    # ---------- shell without write folders: a READ-ONLY tooling option —
    # view mounts and /knowledge are :ro in the container, so the tool is
    # offered and goes through the NORMAL permission gate ----------
    sock3 = os.environ["LOOM_HOME"] + "/fake3.sock"
    cid3, events3, reqs3, finished3 = run_chat(
        root, "fake", sock3, mode="tools", tool_name="shell",
        tool_args={"command": "echo hi"}, answer="deny")   # no folders at all
    kinds3 = [e["kind"] for e in events3]
    check("no-folder shell: loop finished", finished3, str(kinds3))
    check("no-folder shell: tool offered",
          any(t["function"]["name"] == "shell"
              for t in reqs3[0].get("tools", [])))
    check("no-folder shell: gate asked", "tool_wait" in kinds3)
    tr3 = next((e for e in events3 if e["kind"] == "tool_result"), {})
    check("no-folder shell: deny respected",
          tr3.get("ok") is False and "declined" in str(tr3.get("result")), str(tr3))

    # ---------- a mode switch re-decides calls already waiting at the gate
    # (the plumbing chat_set_mode drives: pending_for → permission_for →
    # answer) ----------
    sock4 = os.environ["LOOM_HOME"] + "/fake4.sock"
    server4 = start_fake(sock4, "tools", "write_file",
                         {"path": "/artifacts/x.txt", "content": "hey"})
    mid4 = libconfig.load(root)["models"][0]["id"]
    with srv._lock:
        srv._servers[mid4] = {"id": mid4, "state": "running", "sock": sock4}
    c4 = chats.new_chat(root, "fake")
    c4["messages"].append({"role": "user", "content": "write it"})
    chats.save_chat(root, c4)
    waited4 = threading.Event()
    done4 = threading.Event()
    def push4(ev):
        if ev["kind"] == "tool_wait":
            waited4.set()
        if ev["kind"] in ("done", "error"):
            done4.set()
    chat.send(root, c4["id"], push4)
    check("gate: call parked awaiting permission", waited4.wait(15))
    # a live, UNCANCELLED stream must not be waited out (send stays refused)
    check("cancel-race: live stream refuses the wait",
          chat.wait_if_cancelling(c4["id"], 0.1) is False)
    # the user flips the mode WHILE the call waits — exactly what
    # chat_set_mode does on disk before it re-decides the pending call
    _mid = chats.load_chat(root, c4["id"])
    _mid["permMode"] = "always-allow"
    chats.save_chat(root, _mid)
    pend = chat.GATE.pending_for(c4["id"])
    check("gate: pending call carries its tool name",
          pend and pend[0][1] == "write_file", str(pend))
    cfg4 = libconfig.load(root)
    for _call_id, _tool in pend:
        if libconfig.permission_for(cfg4, _tool, "allow-edits") == "allow":
            chat.GATE.answer(_call_id, "allow")
    check("gate: re-decided call finished", done4.wait(15))
    saved4 = chats.load_chat(root, c4["id"])
    tr4 = next((m for m in saved4["messages"] if m["role"] == "tool"), {})
    check("gate: tool ran after the auto-allow", tr4.get("ok") is True, str(tr4))
    check("gate: mid-wait mode change SURVIVES the loop's saves",
          saved4.get("permMode") == "always-allow", str(saved4.get("permMode")))
    # after the worker exits, a racing send waits through cleanly
    check("cancel-race: finished worker waits through",
          chat.wait_if_cancelling(c4["id"], 5) is True)
    server4.shutdown()

    # ---------- cancel ABORTS a request still waiting for headers ----------
    # (a server mid-prompt sends nothing; stop() must rip the connection
    # down instantly, end the worker quietly, and leave NO error text)
    sock5 = os.environ["LOOM_HOME"] + "/fake5.sock"
    server5 = start_fake(sock5, "stall")
    mid5 = libconfig.load(root)["models"][0]["id"]
    with srv._lock:
        srv._servers[mid5] = {"id": mid5, "state": "running", "sock": sock5}
    c5 = chats.new_chat(root, "fake")
    c5["messages"].append({"role": "user", "content": "stall out"})
    chats.save_chat(root, c5)
    ev5 = []
    done5 = threading.Event()
    def push5(ev):
        ev5.append(ev)
        if ev["kind"] in ("done", "error"):
            done5.set()
    chat.send(root, c5["id"], push5)
    time.sleep(0.7)                    # the request is parked pre-headers
    t0 = time.time()
    chat.stop(c5["id"])
    check("stall-cancel: worker died fast", done5.wait(5), str(ev5))
    check("stall-cancel: under two seconds", time.time() - t0 < 2.0,
          f"{time.time() - t0:.1f}s")
    _errs = [e for e in ev5 if e["kind"] == "error"]
    check("stall-cancel: quiet — no error event, no tracebacks",
          not _errs and not any("AttributeError" in str(e) for e in ev5),
          str(_errs))
    server5.shutdown()

    # ---------- generation progress callback (compaction liveness) ----------
    sockP = os.environ["LOOM_HOME"] + "/fakeP.sock"
    serverP = start_fake(sockP, "plain")
    progress = []
    outP = chat._gen_once("prog-test", {"name": "fake", "host": "", "sock": sockP},
                          [{"role": "user", "content": "hi"}],
                          threading.Event(),
                          on_progress=lambda n: progress.append(n))
    check("gen: progress ticks strictly upward",
          len(progress) >= 2 and progress == sorted(progress)
          and progress[-1] > progress[0], str(progress))
    check("gen: output intact with progress wired",
          outP == "Hello from fake llama", outP)
    serverP.shutdown()

    # ---------- model-generated titles + uniqueness ----------
    sockT1 = os.environ["LOOM_HOME"] + "/fakeT1.sock"
    cidT1, _e1, _r1, _ok1 = run_chat(root, "fake", sockT1, mode="plain",
                                     shutdown_after=False)
    t1 = wait_title(cidT1)
    check("model generates the title", t1 == "Hello from fake llama", t1)
    sockT2 = os.environ["LOOM_HOME"] + "/fakeT2.sock"
    cidT2, _e2, _r2, _ok2 = run_chat(root, "fake", sockT2, mode="plain",
                                     shutdown_after=False)
    t2 = wait_title(cidT2)
    check("duplicate titles get a suffix", t2 == "Hello from fake llama (2)", t2)

    # ---------- compaction: manual trigger + wiring ----------
    sockC = os.environ["LOOM_HOME"] + "/fakeC.sock"
    serverC = start_fake(sockC, "plain")
    midC = libconfig.load(root)["models"][0]["id"]
    with srv._lock:
        srv._servers[midC] = {"id": midC, "state": "running", "sock": sockC}
    cc = chats.new_chat(root, "fake")
    cc["messages"] = [
        {"role": "user", "content": "remember the magic number 42"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "and the color blue"},
        {"role": "assistant", "content": "ok"},
    ]
    chats.save_chat(root, cc)
    evC, doneC = [], threading.Event()

    def pushC(e):
        evC.append(e)
        if e["kind"] in ("compact_done", "compact_error"):
            doneC.set()
    chat.start_compaction(root, cc["id"], pushC)
    check("manual compaction finishes",
          doneC.wait(20) and evC[-1]["kind"] == "compact_done",
          str([e["kind"] for e in evC]))
    savedC = chats.load_chat(root, cc["id"])
    check("compaction marker appended",
          savedC["messages"][-1]["role"] == "compact"
          and savedC["messages"][-1]["replaced"] == 4)
    wireC = chat._wire_messages(root, libconfig.load(root), savedC)
    check("wire carries the summary, not the history",
          any("Hello from fake llama" in str(m.get("content")) for m in wireC)
          and not any("magic number" in str(m.get("content")) for m in wireC),
          str(wireC))

    # ---------- compaction: a server error SURFACES (not 'came back
    # empty') and leaves no marker ----------
    sockE = os.environ["LOOM_HOME"] + "/fakeE.sock"
    serverE = start_fake(sockE, "error")
    with srv._lock:
        srv._servers[midC] = {"id": midC, "state": "running", "sock": sockE}
    ce = chats.new_chat(root, "fake")
    ce["messages"] = [{"role": "user", "content": "hello"},
                      {"role": "assistant", "content": "hi"}]
    chats.save_chat(root, ce)
    evE, doneE = [], threading.Event()

    def pushE(e):
        evE.append(e)
        if e["kind"] in ("compact_done", "compact_error", "compact_cancelled"):
            doneE.set()
    chat.start_compaction(root, ce["id"], pushE)
    check("compaction: server error surfaced with its message",
          doneE.wait(20) and evE[-1]["kind"] == "compact_error"
          and "kaboom" in str(evE[-1].get("msg")), str(evE[-1:]))
    check("compaction: failed run leaves no marker",
          all(m["role"] != "compact"
              for m in chats.load_chat(root, ce["id"])["messages"]))
    serverE.shutdown()

    # ---------- compaction: cancellable even while the server stalls
    # pre-headers (the 'no stop button' failure) ----------
    sockF = os.environ["LOOM_HOME"] + "/fakeF.sock"
    serverF = start_fake(sockF, "stall")
    with srv._lock:
        srv._servers[midC] = {"id": midC, "state": "running", "sock": sockF}
    cf = chats.new_chat(root, "fake")
    cf["messages"] = [{"role": "user", "content": "hello"},
                      {"role": "assistant", "content": "hi"}]
    chats.save_chat(root, cf)
    evF, doneF = [], threading.Event()

    def pushF(e):
        evF.append(e)
        if e["kind"] in ("compact_done", "compact_error", "compact_cancelled"):
            doneF.set()
    chat.start_compaction(root, cf["id"], pushF)
    time.sleep(0.7)                    # request parked pre-headers
    t0F = time.time()
    chat.stop(cf["id"])
    okF = doneF.wait(5)
    check("compaction: cancel lands fast as compact_cancelled",
          okF and time.time() - t0F < 2.0
          and evF[-1]["kind"] == "compact_cancelled",
          str([e["kind"] for e in evF]))
    serverF.shutdown()

    # ---------- compaction at an OVER-FULL window: the request itself is
    # budgeted to fit nctx with generation room to spare ----------
    sockG = os.environ["LOOM_HOME"] + "/fakeG.sock"
    serverG = start_fake(sockG, "plain")
    with srv._lock:
        srv._servers[midC] = {"id": midC, "state": "running", "sock": sockG}
    cg = chats.new_chat(root, "fake")
    cg["messages"] = [{"role": "user", "content": "w " * 20000},  # ~10k tok
                      {"role": "assistant", "content": "ok"},
                      {"role": "user", "content": "FINAL-BREADCRUMB"}]
    chats.save_chat(root, cg)
    evG, doneG = [], threading.Event()

    def pushG(e):
        evG.append(e)
        if e["kind"] in ("compact_done", "compact_error", "compact_cancelled"):
            doneG.set()
    chat.start_compaction(root, cg["id"], pushG)
    check("over-full window still compacts",
          doneG.wait(20) and evG[-1]["kind"] == "compact_done", str(evG[-1:]))
    sentG = serverG.requests[-1]["messages"]
    estG = sum(chat._msg_est(m) for m in sentG)
    check("compaction request fit the window budget",
          estG <= 4096 - 2048 + 64, str(estG))
    check("newest message survived the trim",
          any("FINAL-BREADCRUMB" in str(m.get("content")) for m in sentG))
    markG = chats.load_chat(root, cg["id"])["messages"][-1]
    check("marker carries compaction stats",
          markG["role"] == "compact" and markG.get("tokensBefore", 0) > 0
          and "durMs" in markG, str({k: markG.get(k) for k in
                                     ("role", "tokensBefore", "durMs")}))
    check("original oversize message untouched on disk",
          len(chats.load_chat(root, cg["id"])["messages"][0]["content"])
          == 40000)
    serverG.shutdown()

    # ---------- auto-compaction at the threshold ----------
    sockD = os.environ["LOOM_HOME"] + "/fakeD.sock"
    serverD = start_fake(sockD, "plain")
    with srv._lock:
        srv._servers[midC] = {"id": midC, "state": "running", "sock": sockD}
    cd = chats.new_chat(root, "fake")
    # ctx is 4096, threshold 0.8 → ~3277 tokens; ~16k chars trips it
    cd["messages"].append({"role": "user", "content": "x " * 8000})
    cidD, evD, _rD, okD = _drive(root, cd, serverD, "plain", None)
    kindsD = [e["kind"] for e in evD]
    check("auto-compaction fired before the turn",
          okD and "compact_start" in kindsD and "compact_done" in kindsD
          and kindsD.index("compact_done") < kindsD.index("delta"), str(kindsD))
    check("auto-compaction persisted a marker",
          any(m["role"] == "compact"
              for m in chats.load_chat(root, cidD)["messages"]))

    # ---------- archive semantics ----------
    chats.set_archived(root, cid, True)
    check("archived flag", chats.load_chat(root, cid)["archived"] is True)
    check("list shows archived", any(c["id"] == cid and c["archived"]
                                     for c in chats.list_chats(root)))

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
