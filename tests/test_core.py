"""Core module tests: store recents, library sandbox, config parsing,
search ranking. Script-style: run with `uv run python tests/test_core.py`;
nonzero exit on failure."""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import libconfig, library, search, store  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- store: recents ----------
store.touch_recent("/tmp/lib-a")
store.touch_recent("/tmp/lib-b")
rec = store.visible_recents()
check("recents ordered newest first", [r["path"] for r in rec] == ["/tmp/lib-b", "/tmp/lib-a"], str(rec))

store.remove_recent("/tmp/lib-b", omit=True)
check("omitted entry hidden", [r["path"] for r in store.visible_recents()] == ["/tmp/lib-a"])

store.touch_recent("/tmp/lib-b")   # opening an omitted library
check("omitted entry stays hidden after reopen",
      [r["path"] for r in store.visible_recents()] == ["/tmp/lib-b" ] if False else
      "/tmp/lib-b" not in [r["path"] for r in store.visible_recents()])

store.clear_recents()
check("clear keeps only omissions", store.visible_recents() == [])

# ---------- store: per-library sessions ----------
check("no session yet", store.session_get("/tmp/lib-a") is None)
sess = {"tabs": [{"type": "library", "chatId": None},
                 {"type": "chat", "chatId": "abc123"}],
        "activeTab": "chat:abc123",
        "lib": {"open": "loom.yaml", "expanded": {"prompts": False},
                "leftWidth": 300}}
store.session_set("/tmp/lib-a", sess)
check("session roundtrip", store.session_get("/tmp/lib-a") == sess)
store.session_set("/tmp/lib-b", {"tabs": []})
check("sessions are per-library",
      store.session_get("/tmp/lib-a") == sess
      and store.session_get("/tmp/lib-b") == {"tabs": []})
store.session_set("/tmp/lib-a", "garbage")
check("garbage session sanitized", store.session_get("/tmp/lib-a") == {})
store.clear_recents()
check("clearing recents keeps sessions",
      store.session_get("/tmp/lib-b") == {"tabs": []})

# ---------- store: per-library alerts ----------
check("no alerts yet", store.alerts_get("/tmp/lib-a") == [])
store.alerts_add("/tmp/lib-a", {"ts": 111, "level": "err", "msg": "boom"})
store.alerts_add("/tmp/lib-a", {"level": "ok", "msg": "fine"})
al = store.alerts_get("/tmp/lib-a")
check("alerts kept oldest first", [a["msg"] for a in al] == ["boom", "fine"], str(al))
check("alert defaults filled",
      al[0]["ts"] == 111 and al[0]["level"] == "err" and al[1]["ts"] > 0)
store.alerts_add("/tmp/lib-b", {"msg": "other"})
check("alerts are per-library",
      [a["msg"] for a in store.alerts_get("/tmp/lib-b")] == ["other"])
store.alerts_add("/tmp/lib-a", {"msg": ""})
check("empty alert dropped", len(store.alerts_get("/tmp/lib-a")) == 2)
store.alerts_add("/tmp/lib-a", "garbage")
check("garbage alert dropped", len(store.alerts_get("/tmp/lib-a")) == 2)
store.alerts_clear("/tmp/lib-a")
check("clear is per-library",
      store.alerts_get("/tmp/lib-a") == []
      and len(store.alerts_get("/tmp/lib-b")) == 1)

# ---------- store: reasoning preferences ----------
check("no reasoning prefs yet", store.reasoning_all("/tmp/lib-a") == {})
store.reasoning_set("/tmp/lib-a", "m1", {"method": "effort", "level": "xhigh"})
store.reasoning_set("/tmp/lib-a", "m2", {"method": "template", "level": "off"})
check("reasoning prefs stored",
      store.reasoning_get("/tmp/lib-a", "m1") == {"method": "effort", "level": "xhigh"}
      and store.reasoning_get("/tmp/lib-a", "m2") == {"method": "template", "level": "off"})
check("reasoning is per-library", store.reasoning_all("/tmp/lib-b") == {})
store.reasoning_set("/tmp/lib-a", "m1", None)
check("clearing restores the default",
      store.reasoning_get("/tmp/lib-a", "m1") is None
      and store.reasoning_get("/tmp/lib-a", "m2") is not None)
try:
    store.reasoning_set("/tmp/lib-a", "m3", {"method": "bogus", "level": "high"})
    check("bad reasoning pref rejected", False)
except ValueError:
    check("bad reasoning pref rejected", True)

# ---------- reasoning pref → request shape ----------
from loom import chat as _chatmod  # noqa: E402
_b = {"messages": [{"role": "system", "content": "sys"}]}
_chatmod._apply_reasoning(_b, None)
check("no pref touches nothing",
      "reasoning_effort" not in _b and "chat_template_kwargs" not in _b
      and _b["messages"][0]["content"] == "sys")
_chatmod._apply_reasoning(_b, {"method": "effort", "level": "xhigh"})
check("effort sets the request field", _b["reasoning_effort"] == "xhigh")
_chatmod._apply_reasoning(_b, {"method": "effort", "level": "off"})
check("effort off → 'none' (llama-server's disable value)",
      _b["reasoning_effort"] == "none")
store.reasoning_set("/tmp/lib-a", "m9", {"method": "effort", "level": "minimal"})
check("minimal is a valid stored level",
      store.reasoning_get("/tmp/lib-a", "m9") == {"method": "effort",
                                                  "level": "minimal"})
_chatmod._apply_reasoning(_b, {"method": "template", "level": "on"})
check("template on → boolean true",
      _b["chat_template_kwargs"] == {"enable_thinking": True})
_chatmod._apply_reasoning(_b, {"method": "template", "level": "off"})
check("template off → boolean false",
      _b["chat_template_kwargs"] == {"enable_thinking": False})
_chatmod._apply_reasoning(_b, {"method": "prompt", "level": "off"})
check("prompt switch lands on the system message",
      _b["messages"][0]["content"].endswith("/no_think"))

# ---------- store: pinned models ----------
check("no pins yet", store.pins_all("/tmp/lib-a") == [])
store.pin_set("/tmp/lib-a", "m1", True)
store.pin_set("/tmp/lib-a", "m2", True)
check("pins keep pin order", store.pins_all("/tmp/lib-a") == ["m1", "m2"])
check("pin is idempotent",
      store.pin_set("/tmp/lib-a", "m1", True) == ["m1", "m2"])
check("unpin removes", store.pin_set("/tmp/lib-a", "m1", False) == ["m2"])
check("pins are per-library", store.pins_all("/tmp/lib-b") == [])

# ---------- store: ~ display paths ----------
home_dir = str(Path("~").expanduser())
check("home path collapses to ~",
      store.display_path(home_dir + "/repos/x") == "~/repos/x"
      and store.display_path(home_dir) == "~"
      and store.display_path("/srv/data") == "/srv/data"
      and store.display_path(home_dir + "sneaky") == home_dir + "sneaky")

# ---------- chats: artifact purge on delete ----------
from loom import chats  # noqa: E402
_aroot = Path(tempfile.mkdtemp(prefix="loomtest-lib-"))
_adir = chats.artifacts_dir(_aroot, "abc123", create=True)
(_adir / "report.md").write_text("hi", encoding="utf-8")
check("artifacts dir created", _adir.is_dir())
chats.delete_chat(_aroot, "abc123")
check("permanent delete purges artifacts", not _adir.exists())

# ---------- environments: defs in the library, secrets in a fake keyring ----------
from loom import envs as envsmod  # noqa: E402
_fakekr = {}
envsmod._kr_get = lambda n: _fakekr.get(n)
envsmod._kr_set = lambda n, b: _fakekr.__setitem__(n, b)
envsmod._kr_del = lambda n: _fakekr.pop(n, None)
_eroot = Path(tempfile.mkdtemp(prefix="loomtest-env-"))
check("no environments yet", envsmod.list_envs(_eroot) == [])
envsmod.save_env(_eroot, "aws-dev",
                 [{"key": "AWS_REGION", "value": "us-east-1", "secret": False},
                  {"key": "AWS_SECRET_ACCESS_KEY", "secret": True}],
                 {"AWS_SECRET_ACCESS_KEY": "hunter2"})
check("environment listed", envsmod.list_envs(_eroot) == ["aws-dev"])
_ftxt = (_eroot / "environments.yaml").read_text()
check("secret VALUE never in the library file", "hunter2" not in _ftxt, _ftxt)
check("secret STUB is in the library file", "AWS_SECRET_ACCESS_KEY" in _ftxt)
check("plain value is in the library file", "us-east-1" in _ftxt)
_vars, _missing = envsmod.resolve(_eroot, "aws-dev")
check("resolve merges plain + keyring",
      _vars == {"AWS_REGION": "us-east-1",
                "AWS_SECRET_ACCESS_KEY": "hunter2"} and _missing == [],
      str((_vars, _missing)))
check("secret status reports set",
      envsmod.secret_status(_eroot, "aws-dev") == {"AWS_SECRET_ACCESS_KEY": True})
envsmod.set_secrets("aws-dev", {"AWS_SECRET_ACCESS_KEY": ""})   # clear
_vars, _missing = envsmod.resolve(_eroot, "aws-dev")
check("missing secret stays UNSET and is reported",
      "AWS_SECRET_ACCESS_KEY" not in _vars
      and _missing == ["AWS_SECRET_ACCESS_KEY"], str((_vars, _missing)))
try:
    envsmod.save_env(_eroot, "bad", [{"key": "1BAD", "secret": False}], {})
    check("bad variable name rejected", False)
except envsmod.EnvError:
    check("bad variable name rejected", True)
envsmod.delete_env(_eroot, "aws-dev")
check("environment deleted (file + keyring)",
      envsmod.list_envs(_eroot) == [] and _fakekr == {}, str(_fakekr))

# ---------- library: one Loom per library (.loom-pid lock) ----------
_lroot = Path(tempfile.mkdtemp(prefix="loomtest-lock-"))
library.acquire_lock(_lroot)
check("lock file written",
      (_lroot / ".loom-pid").read_text().strip() == str(os.getpid()))
library.acquire_lock(_lroot)   # same process re-acquires freely
check("own lock re-acquired", True)
_orig_alive = library._lock_holder_alive
library._lock_holder_alive = lambda pid: True   # another LIVE Loom holds it
(_lroot / ".loom-pid").write_text("99999")
try:
    library.acquire_lock(_lroot)
    check("second instance refused", False)
except library.LibraryError as e:
    check("second instance refused", "another Loom" in str(e), str(e))
library._lock_holder_alive = lambda pid: False  # ...that instance died
library.acquire_lock(_lroot)
check("stale lock taken over",
      (_lroot / ".loom-pid").read_text().strip() == str(os.getpid()))
library._lock_holder_alive = _orig_alive
(_lroot / ".loom-pid").write_text("99999")      # not ours anymore
library.release_lock(_lroot)
check("release never deletes another instance's lock",
      (_lroot / ".loom-pid").exists())
(_lroot / ".loom-pid").write_text(str(os.getpid()))
library.release_lock(_lroot)
check("own lock released", not (_lroot / ".loom-pid").exists())
check("dead-pid probe honest", library._lock_holder_alive(2 ** 22 + 12345) is False)

# ---------- library: git branch for the attachment pills ----------
_g = Path(tempfile.mkdtemp(prefix="loomtest-git-"))
check("non-repo has no branch", library.git_branch(_g) is None)
(_g / ".git").mkdir()
(_g / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
check("branch read from HEAD", library.git_branch(_g) == "feature/x")
(_g / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
check("detached HEAD shows a short sha", library.git_branch(_g) == "0123456")

# ---------- library ----------
with tempfile.TemporaryDirectory() as d:
    root = library.create_library(d + "/lib")
    check("library scaffolded", library.is_library(root))
    check("re-create opens existing", library.create_library(str(root)) == root)

    (root / "junk").mkdir()
    (root / "junk" / "x.txt").write_text("hello needle world")
    try:
        library.safe_join(root, "../escape")
        check("safe_join blocks escape", False)
    except library.LibraryError:
        check("safe_join blocks escape", True)

    library.write_file(root, "knowledge/note.md", "# Needle\nfindme here")
    got = library.read_file(root, "knowledge/note.md")
    check("write/read roundtrip", got["text"].startswith("# Needle"))

    library.rename_entry(root, "knowledge/note.md", "knowledge/note2.md")
    check("rename works", (root / "knowledge/note2.md").is_file())
    library.delete_entry(root, "junk")
    check("delete folder works", not (root / "junk").exists())

    # search: file fuzzy + content
    hits = search.search(root, "note2")
    check("fuzzy file hit", any(h["kind"] == "file" and h["rel"].endswith("note2.md") for h in hits), str(hits[:3]))
    hits = search.search(root, "findme")
    check("content line hit", any(h["kind"] == "line" and h.get("line") == 2 for h in hits), str(hits[:3]))

    # config — permission modes
    cfg = libconfig.load(root)
    check("builtin modes present",
          {"always-ask", "allow-edits", "always-allow"}
          <= set(cfg["permissionModes"]))
    check("always-ask asks for shell",
          libconfig.permission_for(cfg, "shell", "always-ask") == "ask")
    check("always-ask allows reads",
          libconfig.permission_for(cfg, "grep", "always-ask") == "allow")
    check("allow-edits allows edit_file",
          libconfig.permission_for(cfg, "edit_file", "allow-edits") == "allow")
    check("allow-edits allows shell (the container is the boundary)",
          libconfig.permission_for(cfg, "shell", "allow-edits") == "allow")

    # ---------- the stamped yamls state the REAL levels: every template's
    # explicit table must agree with BUILTIN_MODES (no silent drift) ----------
    _tpl_base = Path(__file__).resolve().parent.parent / "loom" / "templates"
    _sources = [(t.name, (_tpl_base / t.name / "loom.yaml").read_text())
                for t in sorted(_tpl_base.iterdir())
                if (t / "loom.yaml").is_file()]
    _sources.append(("DEFAULT_LOOM_YAML", library.DEFAULT_LOOM_YAML))
    for _name, _text in _sources:
        _troot = Path(tempfile.mkdtemp(prefix="loomtest-tpl-"))
        (_troot / "loom.yaml").write_text(_text)
        _tcfg = libconfig.load(_troot)
        _drift = [(mo, tool, libconfig.permission_for(_tcfg, tool, mo), lv)
                  for mo, table in libconfig.BUILTIN_MODES.items()
                  for tool, lv in table.items()
                  if libconfig.permission_for(_tcfg, tool, mo) != lv]
        check(f"{_name} yaml modes match the built-ins", not _drift, str(_drift))
        # the RAW yaml must spell out every tool level itself — empty
        # tools:{} blocks hiding the real table behind code is the bug
        # this guards against
        import yaml as _yamlmod
        _raw = _yamlmod.safe_load(_text)["permission-modes"]
        _explicit = all(
            set(( _raw.get(mo) or {}).get("tools") or {}) >= set(table)
            for mo, table in libconfig.BUILTIN_MODES.items())
        check(f"{_name} lists every tool level explicitly", _explicit,
              str({mo: (_raw.get(mo) or {}).get("tools")
                   for mo in libconfig.BUILTIN_MODES}))
    check("always-allow allows shell",
          libconfig.permission_for(cfg, "shell", "always-allow") == "allow")
    (root / "loom.yaml").write_text("models:\n- name: m1\n  model: /x.gguf\n  context: 4096\n  mmproj: /mm.gguf\n")
    cfg = libconfig.load(root)
    check("model parsed", cfg["models"][0]["ctx"] == 4096 and cfg["models"][0]["mmproj"] == "/mm.gguf")
    from loom import srv
    args = srv.compose_args(cfg["models"][0])
    check("mmproj in argv", "--mmproj" in args and args[args.index("--mmproj") + 1] == "/mm.gguf")
    check("jinja appended", args[-1] == "--jinja")

    (root / "loom.yaml").write_text("models: {not: a list}\n")
    try:
        libconfig.load(root)
        check("bad config rejected", False)
    except libconfig.ConfigError:
        check("bad config rejected", True)

    # custom mode + disabled level + legacy shape
    (root / "loom.yaml").write_text(
        "permission-modes:\n"
        "  read-only:\n"
        "    tools:\n"
        "      shell: disabled\n"
        "      edit_file: disabled\n"
        "      write_file: disabled\n"
        "  always-ask:\n"
        "    tools:\n"
        "      grep: ask\n"
        "permissions:\n"
        "  tools:\n"
        "    knowledge_search: deny\n")
    cfg = libconfig.load(root)
    check("custom mode defined",
          libconfig.permission_for(cfg, "shell", "read-only") == "disabled")
    check("custom mode inherits defaults",
          libconfig.permission_for(cfg, "read_file", "read-only") == "allow")
    check("builtin override applied",
          libconfig.permission_for(cfg, "grep", "always-ask") == "ask")
    check("legacy permissions merge into always-ask",
          libconfig.permission_for(cfg, "knowledge_search", "always-ask") == "deny")
    from loom import chat as chatmod
    names = [t["function"]["name"]
             for t in chatmod.tool_specs(cfg, "read-only")]
    check("disabled tools not offered",
          "shell" not in names and "edit_file" not in names
          and "read_file" in names, str(names))

    (root / "loom.yaml").write_text(
        "permission-modes:\n  x:\n    tools:\n      shell: sometimes\n")
    try:
        libconfig.load(root)
        check("bad permission level rejected", False)
    except libconfig.ConfigError:
        check("bad permission level rejected", True)

    (root / "loom.yaml").write_text("chat:\n  permission_mode: nope\n")
    try:
        libconfig.load(root)
        check("unknown default mode rejected", False)
    except libconfig.ConfigError:
        check("unknown default mode rejected", True)
    (root / "loom.yaml").write_text("models: []\n")   # restore sanity

    # ---------- programming tools (grep / find_files / read gate / edit) ----------
    import threading
    from loom import chats as chatsmod
    work = Path(d) / "proj"
    (work / "src").mkdir(parents=True)
    (work / "src" / "main.py").write_text("def hello():\n    return 'needle42'\n")
    (work / "big.txt").write_text("line\n" * 30000)   # ~150k chars
    chat_doc = {"id": "t1", "folders": [{"path": str(work), "mode": "write"}]}
    cancel = threading.Event()
    cfg = libconfig.load(root)

    names_all = [t["function"]["name"]
                 for t in chatmod.tool_specs(cfg, "always-allow")]
    check("shell + write tools always offered (/artifacts is rw)",
          "shell" in names_all and "write_file" in names_all
          and "edit_file" in names_all, str(names_all))

    # /artifacts: the model's delivery folder — writable via file tools
    out = chatmod._exec_tool(root, cfg, {"id": "t1", "folders": []},
                             "write_file",
                             {"path": "/artifacts/notes.md", "content": "hi"},
                             threading.Event())
    check("write_file lands in /artifacts",
          (chatsmod.artifacts_dir(root, "t1") / "notes.md").read_text() == "hi",
          out)
    recs = chatmod._artifact_records(root, "t1")
    check("artifact record surfaced",
          [r["name"] for r in recs] == ["notes.md"] and recs[0]["bytes"] == 2,
          str(recs))
    (chatsmod.artifacts_dir(root, "t1") / "uploads").mkdir()
    check("uploads folder not echoed back",
          [r["name"] for r in chatmod._artifact_records(root, "t1")]
          == ["notes.md"])

    out = chatmod._exec_tool(root, cfg, chat_doc, "find_files",
                             {"query": "mainpy"}, cancel)
    check("find_files fuzzy filename", "/mnt/proj/src/main.py" in out, out)
    out = chatmod._exec_tool(root, cfg, chat_doc, "grep",
                             {"query": "needle42"}, cancel)
    check("grep finds content with line no",
          "/mnt/proj/src/main.py:2:" in out, out)
    try:
        chatmod._exec_tool(root, cfg, chat_doc, "read_file",
                           {"path": "/mnt/proj/big.txt"}, cancel)
        check("read gate blocks huge whole-file read", False)
    except chatsmod.ChatError as e:
        check("read gate blocks huge whole-file read", "offset" in str(e), str(e))
    out = chatmod._exec_tool(root, cfg, chat_doc, "read_file",
                             {"path": "/mnt/proj/big.txt", "offset": 5, "limit": 2},
                             cancel)
    check("sliced read works", out.startswith("[lines 5-6 of"), out[:40])
    out = chatmod._exec_tool(root, cfg, chat_doc, "edit_file",
                             {"path": "/mnt/proj/src/main.py",
                              "old_string": "needle42", "new_string": "done"},
                             cancel)
    check("edit_file replaces",
          "done" in (work / "src" / "main.py").read_text())
    (work / "dup.txt").write_text("aa aa")
    try:
        chatmod._exec_tool(root, cfg, chat_doc, "edit_file",
                           {"path": "/mnt/proj/dup.txt",
                            "old_string": "aa", "new_string": "b"}, cancel)
        check("edit_file demands uniqueness", False)
    except chatsmod.ChatError as e:
        check("edit_file demands uniqueness", "unique" in str(e), str(e))
    out = chatmod._exec_tool(root, cfg, chat_doc, "edit_file",
                             {"path": "/mnt/proj/dup.txt", "old_string": "aa",
                              "new_string": "b", "replace_all": True}, cancel)
    check("edit_file replace_all", (work / "dup.txt").read_text() == "b b")
    ro_chat = {"id": "t2", "folders": [{"path": str(work), "mode": "view"}]}
    try:
        chatmod._exec_tool(root, cfg, ro_chat, "edit_file",
                           {"path": "/mnt/proj/dup.txt",
                            "old_string": "b", "new_string": "c",
                            "replace_all": True}, cancel)
        check("edit_file refused on view-mode folder", False)
    except chatsmod.ChatError:
        check("edit_file refused on view-mode folder", True)

    # ---------- wire: thought truncation + compaction markers ----------
    check("compaction defaults on",
          cfg["chat"]["compaction"]["auto"] is True
          and cfg["chat"]["compaction"]["threshold"] == 0.8
          and cfg["chat"]["thought_truncation"] is True)
    doc = {"id": "w1", "folders": [], "messages": [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1", "thinking": "t1"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "a2", "thinking": "t2"},
    ]}
    wire = chatmod._wire_messages(root, cfg, doc)
    asst = [m for m in wire if m["role"] == "assistant"]
    check("older thought dropped from wire", "<think>" not in asst[0]["content"])
    check("last thought kept on wire", asst[1]["content"].startswith("<think>\nt2"))
    doc["messages"].insert(2, {"role": "compact", "content": "SUMMARY-XYZ",
                               "replaced": 2})
    wire = chatmod._wire_messages(root, cfg, doc)
    check("compaction summary wired",
          any("SUMMARY-XYZ" in str(m.get("content")) for m in wire))
    check("pre-compaction messages left the wire",
          not any(m.get("content") == "one" for m in wire))
    # environment signals: knowledge base auto-detail + UTC stamps
    doc2 = {"id": "w2", "folders": [], "messages": [
        {"role": "user", "content": "hi", "ts": 1787743945000}]}
    wire2 = chatmod._wire_messages(root, cfg, doc2)
    sysmsg = wire2[0]["content"]
    check("env details the knowledge base",
          "## Knowledge base" in sysmsg and "knowledge_search" in sysmsg
          and "read_file" in sysmsg)
    check("env maps knowledge contents", "knowledge/" in sysmsg, sysmsg[-500:])
    check("env carries current UTC time",
          "Session started" in sysmsg and "UTC" in sysmsg)
    check("user message carries UTC stamp signal",
          wire2[1]["content"].startswith("[2026-")
          and "UTC] hi" in wire2[1]["content"], wire2[1]["content"][:48])
    # PREFIX STABILITY: two wire builds of the same chat must be byte-
    # identical — any per-call variance (a live clock…) breaks the
    # server's prompt cache and forces full reprocessing every turn
    time.sleep(0.05)
    wire2b = chatmod._wire_messages(root, cfg, doc2)
    check("wire is deterministic (prompt-cache friendly)", wire2 == wire2b)

    bd = chatmod.context_breakdown(root, cfg, doc)
    check("context breakdown sane",
          bd["estTokens"] > 0 and bd["parts"]["compacted"] > 0
          and bd["auto"] is True and bd["threshold"] == 0.8, str(bd))

    # ---------- adaptive compaction: turn growth, headroom, usage reset --
    gdoc = {"id": "g1", "folders": [], "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1",
         "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2",
         "usage": {"prompt_tokens": 200, "completion_tokens": 150}},
        {"role": "compact", "content": "S", "replaced": 4},
        {"role": "assistant", "content": "a3",
         "usage": {"prompt_tokens": 80, "completion_tokens": 20}},
        {"role": "assistant", "content": "a4",
         "usage": {"prompt_tokens": 300, "completion_tokens": 100}},
    ]}
    g = chatmod._turn_growth(gdoc)
    check("turn growth from real usage, compaction boundary skipped",
          g == {"avg": 250, "max": 300, "n": 2}, str(g))
    check("headroom covers the worst recent turn (padded avg, 5% floor)",
          chatmod._headroom(gdoc, 4096) == 375
          and chatmod._headroom({"messages": []}, 4000) == 200,
          str(chatmod._headroom(gdoc, 4096)))
    check("last-used tokens scoped to the live slice",
          chatmod._last_used_tokens(gdoc) == 400
          and chatmod._last_used_tokens({"id": "g2", "messages": [
              {"role": "assistant", "content": "a",
               "usage": {"prompt_tokens": 900, "completion_tokens": 100}},
              {"role": "compact", "content": "S", "replaced": 1},
              {"role": "user", "content": "next"}]}) == 0)
    bd2 = chatmod.context_breakdown(root, cfg, gdoc)
    check("breakdown carries the adaptive stats",
          bd2["turnAvg"] == 250 and bd2["turnMax"] == 300
          and bd2["turnSamples"] == 2, str(bd2))

    # ---------- compaction request is budgeted (never exceeds the window)
    big = {"id": "big1", "folders": [], "messages": [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "calling",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "y" * 40_000},
        {"role": "user", "content": "z " * 30_000},
        {"role": "user", "content": "the end marker"},
    ]}
    msgs, dropped = chatmod._compact_request(root, cfg, big, 4096)
    est = sum(chatmod._msg_est(m) for m in msgs)
    check("compaction request fits the budget",
          est <= 4096 - 2048 + 64, str(est))
    check("newest message survives, drops are announced",
          any("the end marker" in str(m.get("content")) for m in msgs)
          and (dropped == 0
               or any("omitted" in str(m.get("content")) for m in msgs)))
    check("trimming never mutates the stored chat",
          len(big["messages"][2]["content"]) == 40_000
          and len(big["messages"][3]["content"]) == 60_000)
    msgs_small, dropped_small = chatmod._compact_request(
        root, cfg, {"id": "s1", "folders": [], "messages": [
            {"role": "user", "content": "tiny"}]}, 4096)
    check("small chats compact untrimmed",
          dropped_small == 0
          and any("tiny" in str(m.get("content")) for m in msgs_small))
    (root / "loom.yaml").write_text(
        "chat:\n  compaction:\n    threshold: 1.5\n")
    try:
        libconfig.load(root)
        check("bad compaction threshold rejected", False)
    except libconfig.ConfigError:
        check("bad compaction threshold rejected", True)

# ---------- templates, internals, documentation ----------
tpls = {t["id"]: t for t in library.list_templates()}
check("builtin templates listed",
      {"starter", "developer"} <= set(tpls)
      and tpls["starter"]["builtin"] and tpls["developer"]["description"],
      str(list(tpls)))

with tempfile.TemporaryDirectory() as d:
    root = library.create_library(d + "/lib", "starter")
    check("starter scaffold",
          (root / "prompts" / "system.md").is_file()
          and (root / "containers" / "sandbox.Containerfile").is_file()
          and (root / "internals" / "chats").is_dir()
          and library.is_library(root))
    check("documentation seeded",
          (root / "documentation" / "welcome.md").is_file()
          and (root / "documentation" / "quick-start.md").is_file())
    check("template.txt not copied", not (root / "template.txt").exists())

    # hidden entries: internals + dotfiles out of the tree by default
    (root / ".secret").write_text("x")
    names = [n["name"] for n in library.tree(root)]
    check("internals hidden by default",
          "internals" not in names and ".secret" not in names, str(names))
    shown = library.tree(root, show_hidden=True)
    names2 = [n["name"] for n in shown]
    check("show_hidden reveals them",
          "internals" in names2 and ".secret" in names2, str(names2))
    check("hidden entries flagged",
          all(n["hidden"] for n in shown if n["name"] in ("internals", ".secret")))

    # chats live under internals/chats
    from loom import chats as _ch
    c = _ch.new_chat(root, "")
    check("chats under internals",
          (root / "internals" / "chats" / (c["id"] + ".json")).is_file())

    # search never wanders into internals
    hits = search.search(root, c["id"])
    check("search skips internals", not hits, str(hits[:2]))

with tempfile.TemporaryDirectory() as d:
    root = library.create_library(d + "/dev", "developer")
    check("developer scaffold has knowledge",
          (root / "knowledge" / "programming" / "python" / "uv.md").is_file()
          and (root / "containers" / "dev.Containerfile").is_file())
    cfg = libconfig.load(root)
    check("developer default container is dev",
          cfg["containers"]["default"] == "dev"
          and any(dd["name"] == "dev" for dd in cfg["containers"]["definitions"]))
    check("unified voice in knowledge READMEs",
          "For agents" not in (root / "knowledge" / "programming" / "README.md").read_text())
    from loom import chat as chatmod2
    devsys = chatmod2._wire_messages(root, cfg, {"id": "d", "folders": [],
                                                 "messages": []})[0]["content"]
    check("developer env maps the knowledge tree",
          "knowledge/programming/" in devsys and "python" in devsys
          and "go" in devsys, devsys[-400:])

with tempfile.TemporaryDirectory() as d:
    # legacy layout migrates on open: chats/ → internals/chats/
    root = library.create_library(d + "/old", "starter")
    legacy = root / "chats"
    legacy.mkdir()
    (legacy / "abc.json").write_text('{"id": "abc", "messages": []}')
    library.open_library(str(root))
    check("legacy chats migrated",
          (root / "internals" / "chats" / "abc.json").is_file()
          and not legacy.exists())

# user templates in ~/.loom/templates
ut = library.user_templates_dir() / "my-team"
(ut / "prompts").mkdir(parents=True)
(ut / "prompts" / "system.md").write_text("# team prompt")
(ut / "template.txt").write_text("Our team setup")
tpls = {t["id"]: t for t in library.list_templates()}
check("user template discovered",
      "my-team" in tpls and not tpls["my-team"]["builtin"]
      and tpls["my-team"]["description"] == "Our team setup")
with tempfile.TemporaryDirectory() as d:
    root = library.create_library(d + "/team", "my-team")
    check("user template applied + fallback loom.yaml",
          (root / "prompts" / "system.md").read_text() == "# team prompt"
          and (root / "loom.yaml").is_file())

# ---------- signal policy: one signal notifies, spam forces ----------
from loom.app import make_signal_handler  # noqa: E402

_sig_log = []
_now = [0.0]
h = make_signal_handler(lambda: _sig_log.append("notify"),
                        lambda: _sig_log.append("force"),
                        window=3.0, _clock=lambda: _now[0])
h()                       # t=0: first signal → confirm dialog
check("single signal notifies", _sig_log == ["notify"], str(_sig_log))
_now[0] = 100.0
h()                       # long after → isolated again, still just notify
check("stray signal much later notifies again",
      _sig_log == ["notify", "notify"], str(_sig_log))
_now[0] = 101.5
h()                       # second within the window → the user means it
check("rapid second signal forces exit",
      _sig_log == ["notify", "notify", "force"], str(_sig_log))
_now[0] = 101.8
h()                       # still spamming → still forcing
check("continued spam keeps forcing", _sig_log[-1] == "force")

# ---------- switching libraries (JsApi level) ----------
from loom.app import Bus, JsApi  # noqa: E402
api = JsApi(Bus(), None)
with tempfile.TemporaryDirectory() as d:
    r = api.library_create(d + "/lib")
    check("switch: library opens", r["ok"], str(r))
    t = {"model": "Some Model", "permMode": "allow-edits",
         "folders": [{"path": "/tmp/proj", "mode": "write"},
                     {"path": "", "mode": "view"}]}
    rc = api.chat_new(t)
    check("chat_new clones template",
          rc["ok"] and rc["data"]["chat"]["model"] == "Some Model"
          and rc["data"]["chat"]["permMode"] == "allow-edits"
          and rc["data"]["chat"]["folders"] == [{"path": "/tmp/proj", "mode": "write"}]
          and rc["data"]["chat"]["messages"] == [], str(rc))
    rc2 = api.chat_new(None)
    check("chat_new without template stays plain",
          rc2["ok"] and rc2["data"]["chat"]["folders"] == []
          and "permMode" not in rc2["data"]["chat"], str(rc2))
    ctx0 = rc2["data"]["chat"].get("context")
    check("fresh chat carries a context breakdown",
          isinstance(ctx0, dict) and ctx0["estTokens"] > 0
          and ctx0["lastUsedTokens"] == 0
          and isinstance(ctx0["parts"], dict), str(ctx0)[:200])
    check("breakdown parts are ints, never null",
          all(isinstance(v, int) for v in ctx0["parts"].values()), str(ctx0["parts"]))

    # ---------- context diagnostics rows ----------
    _dc = chats.load_chat(api._need_root(), rc2["data"]["chat"]["id"])
    _dc["messages"] = [
        {"role": "user", "content": "hi there", "ts": 1000},
        {"role": "assistant", "content": "hello", "thinking": "let me think",
         "ts": 2000,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "grep",
         "content": "no matches", "ok": True, "ts": 3000},
        {"role": "compact", "content": "summary", "replaced": 2, "ts": 4000},
    ]
    chats.save_chat(api._need_root(), _dc)
    rd = api.chat_diag(_dc["id"])
    _kinds = [r["kind"] for r in rd["data"]["rows"]] if rd["ok"] else []
    check("diag: rows split user/think/assistant/tool/compact",
          _kinds == ["user", "think", "assistant", "tool", "compact"],
          str(_kinds))
    check("diag: every row carries tokens + preview",
          rd["ok"] and all(r["tokens"] > 0 and r["preview"]
                           for r in rd["data"]["rows"]), str(rd)[:300])
    _drows = rd["data"]["rows"]
    check("diag: durations — user instant, tool from ts gap, "
          "assistant split across think+body",
          _drows[0]["durMs"] == 0 and _drows[3]["durMs"] == 1000
          and (_drows[1]["durMs"] + _drows[2]["durMs"]) == 1000,
          str([r["durMs"] for r in _drows]))
    check("diag: tool row names its tool",
          rd["ok"] and rd["data"]["rows"][3]["extra"].startswith("grep"))

    # per-chat container network access (off by default)
    from loom import chats as chmod2
    rt2 = api._need_root()
    cid_net = rc2["data"]["chat"]["id"]
    check("network off by default",
          not chmod2.load_chat(rt2, cid_net).get("network"))
    rn = api.chat_set_network(cid_net, True)
    check("network toggles on",
          rn["ok"] and rn["data"]["network"] is True
          and chmod2.load_chat(rt2, cid_net)["network"] is True, str(rn))
    rn = api.chat_set_network(cid_net, False)
    check("network toggles off", rn["ok"] and rn["data"]["network"] is False)
    rc_net = api.chat_new({"network": True})
    check("chat_new clones network",
          rc_net["ok"] and rc_net["data"]["chat"].get("network") is True)
    rc3 = api.chat_new({"permMode": "no-such-mode"})
    check("chat_new drops unknown perm mode",
          rc3["ok"] and "permMode" not in rc3["data"]["chat"], str(rc3))
    # windowed history: chat_get with a tail transfers only the last N
    from loom import chats as chmod
    rt = api._need_root()
    cbig = chmod.load_chat(rt, rc2["data"]["chat"]["id"])
    cbig["messages"] = [{"role": "user", "content": f"m{i}"} for i in range(1000)]
    chmod.save_chat(rt, cbig)
    g = api.chat_get(cbig["id"], 200)
    check("chat_get tail windows",
          g["ok"] and len(g["data"]["chat"]["messages"]) == 200
          and g["data"]["chat"]["totalMessages"] == 1000
          and g["data"]["chat"]["messages"][-1]["content"] == "m999", str(g)[:200])
    g2 = api.chat_get(cbig["id"])
    check("chat_get full without tail",
          g2["ok"] and len(g2["data"]["chat"]["messages"]) == 1000)
    g3 = api.chat_get(cbig["id"], 5000)
    check("chat_get tail larger than history",
          g3["ok"] and len(g3["data"]["chat"]["messages"]) == 1000)

    # empty chats are deleted on close, never archived
    ce = api.chat_new(None)["data"]["chat"]
    r_close = api.chat_close(ce["id"])
    check("empty chat deleted on close",
          r_close["ok"] and r_close["data"].get("deleted") is True
          and not any(x["id"] == ce["id"] for x in chmod.list_chats(rt)),
          str(r_close))
    cf = api.chat_new(None)["data"]["chat"]
    cfull = chmod.load_chat(rt, cf["id"])
    cfull["messages"].append({"role": "user", "content": "keep me"})
    chmod.save_chat(rt, cfull)
    r_close2 = api.chat_close(cf["id"])
    check("non-empty chat archives on close",
          r_close2["ok"] and r_close2["data"].get("archived") is True
          and chmod.load_chat(rt, cf["id"])["archived"] is True)
    # stale archived empties purge on library open
    stale = chmod.new_chat(rt, "")
    chmod.set_archived(rt, stale["id"], True)
    api.library_open(str(rt))
    check("stale empty archived chat purged",
          not any(x["id"] == stale["id"] for x in chmod.list_chats(rt)))

    b = api.switch_blockers()
    check("switch blockers shape",
          b["ok"] and b["data"]["servers"] == [] and b["data"]["chats"] == 0,
          str(b))
    r2 = api.library_close()
    check("library_close ok", r2["ok"] and r2["data"]["stopping"] == 0, str(r2))
    st_now = api.app_state()
    check("switch: no library open after close",
          st_now["ok"] and st_now["data"]["library"] is None, str(st_now))
    r3 = api.lib_tree()
    check("closed library refuses file ops", not r3["ok"])

# ---------- models: host list order ----------
from loom import models  # noqa: E402

models.add_host("a@x")
models.add_host("b@y")
models.add_host("c@z")
check("hosts stored in add order", models.hosts() == ["a@x", "b@y", "c@z"])
check("reorder persists",
      models.reorder_hosts(["c@z", "a@x", "b@y"]) == ["c@z", "a@x", "b@y"])
try:
    models.reorder_hosts(["c@z", "a@x"])
    check("reorder refuses a non-permutation", False)
except models.ModelsError:
    check("reorder refuses a non-permutation", True)
try:
    models.reorder_hosts(["c@z", "a@x", "b@y", "d@w"])
    check("reorder refuses an invented host", False)
except models.ModelsError:
    check("reorder refuses an invented host", True)
check("failed reorders left the order intact",
      models.hosts() == ["c@z", "a@x", "b@y"])
for _h in ("a@x", "b@y", "c@z"):
    models.remove_host(_h)

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
