"""Per-chat toggle tests: the artifacts chip (disable /artifacts as a
real boundary) and the thoughts chip (per-chat thought truncation;
loom.yaml only seeds new chats). Script-style: run with
`uv run python tests/test_chat_toggles.py`; nonzero exit on failure."""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-tog-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import chats, libconfig  # noqa: E402
from loom import chat as chatmod  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- tool_specs: artifacts are a TOOL, never a folder ----------
on = json.dumps(chatmod.tool_specs(artifacts=True))
off = json.dumps(chatmod.tool_specs(artifacts=False))
names_on_a = [s["function"]["name"] for s in chatmod.tool_specs(artifacts=True)]
names_off_a = [s["function"]["name"] for s in chatmod.tool_specs(artifacts=False)]
check("no description ever mentions an /artifacts folder",
      "/artifacts" not in on and "/artifacts" not in off)
check("deliver_artifact is offered when delivery is on",
      "deliver_artifact" in names_on_a)
check("the deliver tool vanishes when delivery is off",
      "deliver_artifact" not in names_off_a
      and names_off_a == [n for n in names_on_a if n != "deliver_artifact"])
check("the spec says outbound-only in plain words",
      "OUTBOUND" in on and "ONLY way" in on)

from loom.app import Bus, JsApi  # noqa: E402

api = JsApi(Bus())
with tempfile.TemporaryDirectory(prefix="loomtest-toglib-") as d:
    r = api.library_create(d + "/lib")
    check("library created", r["ok"], str(r))
    rt = api._need_root()
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n")
    cfg = libconfig.load(rt)

    # ---------- artifacts: a delivery CHANNEL, never a place ----------
    import threading as _th
    c = chats.new_chat(rt)
    chats.save_chat(rt, c)
    for state in (False, True):
        if state:
            c["artifactsOff"] = True
            chats.save_chat(rt, c)
        try:
            chatmod._resolve_path(rt, c, "/artifacts/out.md")
            check(f"/artifacts is never a path (off={state})", False)
        except chats.ChatError as e:
            check(f"/artifacts is never a path (off={state})",
                  "deliver_artifact" in str(e), str(e))
    scopes = [p for p, _h in chatmod._search_scopes(rt, c, "/")]
    check("search scopes never include /artifacts",
          all(not s.startswith("/artifacts") for s in scopes), str(scopes))

    # deliver_artifact: refused while off, works when on
    try:
        chatmod._exec_tool(rt, {}, c, "deliver_artifact",
                           {"name": "r.md", "content": "hi"}, _th.Event())
        check("delivery refused while the chip is off", False)
    except chats.ChatError as e:
        check("delivery refused while the chip is off",
              "disabled" in str(e), str(e))
    r = api.chat_set_artifacts(c["id"], True)
    check("the chip re-enables",
          r["ok"] and r["data"]["artifacts"] is True
          and "artifactsOff" not in chats.load_chat(rt, c["id"]), str(r))
    c = chats.load_chat(rt, c["id"])
    out = chatmod._exec_tool(rt, {}, c, "deliver_artifact",
                             {"name": "report.md", "content": "# hi"},
                             _th.Event())
    check("text delivery lands", "delivered report.md" in out, out)
    check("the delivery shows up in the artifact list",
          chatmod._sync_artifacts(rt, c) == ["report.md"])
    # path delivery from the chat home
    from loom import containers as _cont
    home = _cont.chat_home(c["id"])
    (home / "final.bin").write_bytes(b"\x00\x01data")
    out = chatmod._exec_tool(rt, {}, c, "deliver_artifact",
                             {"path": "/home/loom/final.bin"}, _th.Event())
    check("a file from /home/loom delivers (binary ok)",
          "delivered final.bin" in out, out)
    try:
        chatmod._deliver_artifact(rt, c, {"name": "uploads",
                                          "content": "x"})
        check("delivering over uploads/ is refused", False)
    except chats.ChatError:
        check("delivering over uploads/ is refused", True)
    # the /uploads read-only path resolves for file tools
    up = chats.artifacts_dir(rt, c["id"]) / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    (up / "photo.txt").write_text("pix")
    kind, host, mode = chatmod._resolve_path(rt, c, "/uploads/photo.txt")
    check("/uploads resolves read-only",
          kind == "upload" and mode == "view" and host.is_file())
    r = api.chat_set_artifacts(c["id"], False)
    check("the chip disables",
          r["ok"] and r["data"]["artifacts"] is False
          and chats.load_chat(rt, c["id"])["artifactsOff"] is True, str(r))

    # ---------- per-chat thought truncation ----------
    def mk(trunc):
        cc = chats.new_chat(rt)
        if trunc is not None:
            cc["thoughtTruncation"] = trunc
        cc["messages"] = [
            {"role": "user", "content": "u", "ts": 1},
            {"role": "assistant", "content": "a1", "thinking": "t1", "ts": 2},
            {"role": "user", "content": "u2", "ts": 3},
            {"role": "assistant", "content": "a2", "thinking": "t2", "ts": 4},
        ]
        chats.save_chat(rt, cc)
        return chats.load_chat(rt, cc["id"])

    def thinks(cc):
        wire = chatmod._wire_messages(rt, cfg, cc)
        return sum("<think>" in (w.get("content") or "")
                   for w in wire if w["role"] == "assistant")

    check("truncation on: only the latest thought rides", thinks(mk(True)) == 1)
    check("truncation off: every thought rides", thinks(mk(False)) == 2)
    check("no per-chat value falls back to the config default",
          thinks(mk(None)) == 1)

    cc = mk(None)
    r = api.chat_set_thought_truncation(cc["id"], False)
    check("the thoughts chip persists per chat",
          r["ok"] and r["data"]["thoughtTruncation"] is False
          and chats.load_chat(rt, cc["id"])["thoughtTruncation"] is False,
          str(r))
    check("the chip's value drives the wire",
          thinks(chats.load_chat(rt, cc["id"])) == 2)

    # loom.yaml seeds NEW chats only
    (rt / "loom.yaml").write_text(
        "providers:\n- name: ws\n  url: http://127.0.0.1:9\n"
        "chat:\n  thought_truncation: false\n")
    r = api.chat_new(None)
    check("chat_new stamps the loom.yaml default",
          r["ok"] and r["data"]["chat"]["thoughtTruncation"] is False, str(r))
    cfg2 = libconfig.load(rt)
    n = sum("<think>" in (w.get("content") or "")
            for w in chatmod._wire_messages(rt, cfg2, mk(True))
            if w["role"] == "assistant")
    check("an explicit per-chat True beats a False config default",
          n == 1, str(n))

    # ---------- the knowledge-base cut ----------
    kc = chats.new_chat(rt)
    kc["knowledgeOff"] = True
    kc["messages"] = [{"role": "user", "content": "u", "ts": 1}]
    chats.save_chat(rt, kc)
    kc = chats.load_chat(rt, kc["id"])

    names_on = [s["function"]["name"] for s in chatmod.tool_specs()]
    names_off = [s["function"]["name"]
                 for s in chatmod.tool_specs(knowledge=False)]
    check("knowledge off drops the search tool",
          "knowledge_search" in names_on
          and "knowledge_search" not in names_off, str(names_off))
    check("knowledge off scrubs every description",
          "knowledge" not in json.dumps(
              chatmod.tool_specs(knowledge=False)).lower())
    try:
        chatmod._resolve_path(rt, kc, "/knowledge/foo.md")
        check("file tools refuse /knowledge when off", False)
    except chats.ChatError as e:
        check("file tools refuse /knowledge when off",
              "disabled" in str(e), str(e))
    check("search scopes drop /knowledge when off",
          "/knowledge/" not in
          [p for p, _h in chatmod._search_scopes(rt, kc, "/")])
    wire = chatmod._wire_messages(rt, cfg, kc)
    check("Loom's own knowledge injections vanish when off",
          "## Knowledge base" not in wire[0]["content"]
          and "mounted read-only at /knowledge" not in wire[0]["content"]
          and "knowledge_search" not in wire[0]["content"],
          wire[0]["content"][:200])
    r = api.chat_set_knowledge(kc["id"], True)
    check("the chip restores the knowledge base",
          r["ok"] and r["data"]["knowledge"] is True
          and "knowledgeOff" not in chats.load_chat(rt, kc["id"]), str(r))
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, kc["id"]))
    check("restored: the prompt describes the base again",
          "Knowledge base" in wire[0]["content"])
    r = api.chat_set_knowledge(kc["id"], False)
    check("the chip cuts it off again",
          r["ok"] and chats.load_chat(rt, kc["id"])["knowledgeOff"] is True)
    r = api.chat_fork(kc["id"], 0)
    check("a fork carries the knowledge cut",
          r["ok"] and r["data"]["chat"].get("knowledgeOff") is True, str(r))

    # ---------- per-chat MCP tool permission overrides ----------
    mcfg = {"permissionModes": {"always-ask": {"mcp_files_read": "deny"}},
            "chat": {"permission_mode": "always-ask"}}
    check("a chat override beats the permission mode",
          chatmod.perm_for(mcfg, "mcp_files_read", "",
                           {"mcp_files_read": "allow"}) == "allow")
    check("no override falls through to the mode entry",
          chatmod.perm_for(mcfg, "mcp_files_read", "") == "deny")
    check("garbage overrides are ignored",
          chatmod.perm_for(mcfg, "mcp_files_read", "",
                           {"mcp_files_read": "sometimes"}) == "deny")
    mc = chats.new_chat(rt)
    r = api.chat_set_mcp_perm(mc["id"], "mcp_files_read", "disabled")
    check("the MCP override persists",
          r["ok"] and chats.load_chat(rt, mc["id"])["mcpPerms"]
          == {"mcp_files_read": "disabled"}, str(r))
    r = api.chat_set_mcp_perm(mc["id"], "mcp_files_read", "")
    check("an empty level clears the override",
          r["ok"] and "mcpPerms" not in chats.load_chat(rt, mc["id"]))
    r = api.chat_set_mcp_perm(mc["id"], "shell", "deny")
    check("non-MCP tools are refused", not r["ok"], str(r))
    r = api.chat_set_mcp_perm(mc["id"], "mcp_x_y", "sometimes")
    check("bad levels are refused", not r["ok"], str(r))

    # ---------- per-variable env signal hiding ----------
    from loom import envs as _envs
    _okg = _envs._kr_get
    _envs._kr_get = lambda n: None   # no OS keyring in the harness
    (rt / "environments.yaml").write_text(
        "staging:\n  API_URL: https://x\n  TOKEN:\n  REGION: us-1\n")
    ec = chats.new_chat(rt)
    ec["env"] = "staging"
    ec["messages"] = [{"role": "user", "content": "u", "ts": 1}]
    chats.save_chat(rt, ec)
    r = api.chat_set_env_hidden(ec["id"], ["TOKEN", "REGION"])
    check("hidden variables persist",
          r["ok"] and chats.load_chat(rt, ec["id"])["envHidden"]
          == ["REGION", "TOKEN"], str(r))
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, ec["id"]))
    sysc = wire[0]["content"]
    check("hidden variables never reach the prompt",
          "API_URL" in sysc and "REGION" not in sysc
          and "TOKEN" not in sysc, sysc[-300:])
    r = api.chat_set_env_hidden(ec["id"], [])
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, ec["id"]))
    check("clearing restores the names",
          r["ok"] and "REGION" in wire[0]["content"]
          and "envHidden" not in chats.load_chat(rt, ec["id"]))
    _envs._kr_get = _okg

    # ---------- the artifacts prompt section respects the cut ----------
    ac2 = chats.new_chat(rt)
    ac2["messages"] = [{"role": "user", "content": "u", "ts": 1}]
    chats.save_chat(rt, ac2)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, ac2["id"]))
    check("the delivery section is present by default",
          "## Delivering files to the user" in wire[0]["content"]
          and "deliver_artifact" in wire[0]["content"])
    check("no prompt ever describes an /artifacts folder",
          "/artifacts" not in wire[0]["content"])
    api.chat_set_artifacts(ac2["id"], False)
    wire = chatmod._wire_messages(rt, cfg, chats.load_chat(rt, ac2["id"]))
    check("the delivery section is gone when cut",
          "## Delivering files" not in wire[0]["content"]
          and "/artifacts" not in wire[0]["content"])

    # ---------- both settings survive worker refresh and forks ----------
    doc = chats.load_chat(rt, cc["id"])
    api.chat_set_artifacts(cc["id"], False)
    api.chat_set_thought_truncation(cc["id"], True)
    chatmod._refresh_user_fields(rt, doc)
    check("the loop refreshes both toggles from disk",
          doc.get("artifactsOff") is True
          and doc.get("thoughtTruncation") is True, str(doc.get("artifactsOff")))
    api.chat_set_thought_truncation(cc["id"], False)
    r = api.chat_fork(cc["id"], 0)
    fc = r["data"]["chat"]
    check("a fork carries both toggles",
          r["ok"] and fc.get("artifactsOff") is True
          and fc.get("thoughtTruncation") is False, str(r))

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
