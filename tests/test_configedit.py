"""Config editing tests: configedit splicers/serializers and the JsApi
config endpoints (Config tab + section dialogs). Script-style: run with
`uv run python tests/test_configedit.py`; nonzero exit on failure."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-cfg-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from loom import configedit, libconfig  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# =========================================================================
# replace_section
# =========================================================================
DOC = ("# my library\n"
       "providers:\n"
       "- name: ws\n"
       "  url: http://h:1\n"
       "\n"
       "# the chat block\n"
       "chat:\n"
       "  model: old\n"
       "  provider: ws\n"
       "\n"
       "# trailing section\n"
       "api:\n"
       "  port: 4321\n")

t = configedit.replace_section(DOC, ("chat",), ["chat:", "  model: new"])
check("replace_section swaps the block in place",
      "model: new" in t and "model: old" not in t and "provider: ws" not in t, t)
check("replace_section keeps comments around the block",
      "# my library" in t and "# the chat block" in t
      and "# trailing section" in t, t)
check("replace_section keeps the section separator spacing",
      "  model: new\n\n# trailing section" in t, t)
check("replace_section result parses",
      yaml.safe_load(t)["chat"] == {"model": "new"}, t)

t = configedit.replace_section(DOC, ("chat",), [])
check("empty lines remove the section",
      "chat:" not in t and "model:" not in t and "api:" in t, t)
check("removing a missing section is a no-op",
      configedit.replace_section(t, ("chat",), []) == t)

t = configedit.replace_section("a: 1\n", ("containers",),
                               ["containers:", "  engine: docker"])
check("missing section appends with a separating blank",
      t == "a: 1\n\ncontainers:\n  engine: docker\n", t)

t = configedit.replace_section("chat: {model: x}\nb: 2\n", ("chat",),
                               ["chat:", "  model: y"])
check("one-line flow section replaced",
      yaml.safe_load(t) == {"chat": {"model": "y"}, "b": 2}, t)

t = configedit.replace_section(
    "permission_modes:\n  focus:\n    tools:\n      shell: deny\n",
    ("permission-modes", "permission_modes"),
    ["permission-modes:", "  focus:"])
check("alias key spellings both match",
      "permission_modes" not in t and "permission-modes:" in t, t)

# =========================================================================
# list items (providers / mcp-servers)
# =========================================================================
LDOC = ("# head\n"
        "providers:\n"
        "- name: a\n"
        "  vendor: llama-cpp\n"
        "  url: http://a:1\n"
        "# b's comment\n"
        "- name: b\n"
        "  url: http://b:1\n"
        "  ssh: sam@b\n"
        "- name: c\n"
        "  url: http://c:1\n"
        "chat:\n"
        "  model: m\n")

t = configedit.replace_list_item(LDOC, ("providers",), "b",
                                 ["- name: b2", "  vendor: openai",
                                  "  url: http://b:2"])
got = yaml.safe_load(t)
check("replace_list_item swaps just that item",
      [p["name"] for p in got["providers"]] == ["a", "b2", "c"]
      and got["providers"][1]["vendor"] == "openai"
      and "ssh" not in got["providers"][1], t)
check("comments outside the item survive",
      "# head" in t and "# b's comment" in t, t)
check("the other items stay byte-for-byte",
      "  url: http://a:1\n" in t and "- name: c\n  url: http://c:1\n" in t, t)

t = configedit.remove_list_item(LDOC, ("providers",), "b")
got = yaml.safe_load(t)
check("remove_list_item deletes just that item",
      [p["name"] for p in got["providers"]] == ["a", "c"], t)

t = configedit.remove_list_item(
    "providers:\n- name: only\n  url: http://h:1\nchat:\n  model: m\n",
    ("providers",), "only")
check("removing the last item leaves a bare parsable key",
      yaml.safe_load(t) == {"providers": None, "chat": {"model": "m"}}
      and libconfig.parse_text(t)["providers"] == [], t)

IND = ("providers:\n"
       "  - name: x\n"
       "    url: http://x:1\n"
       "  - name: y\n"
       "    url: http://y:1\n")
t = configedit.replace_list_item(IND, ("providers",), "y",
                                 ["- name: y", "  url: http://y:2"])
check("an indented list keeps its indentation",
      "  - name: y\n    url: http://y:2\n" in t, t)

try:
    configedit.replace_list_item(LDOC, ("providers",), "nope", ["- name: n"])
    check("missing item raises", False)
except ValueError as e:
    check("missing item raises", "nope" in str(e), str(e))
try:
    configedit.remove_list_item("a: 1\n", ("providers",), "x")
    check("missing section raises", False)
except ValueError:
    check("missing section raises", True)

# =========================================================================
# serializers (round-tripped through parse_text)
# =========================================================================
check("all-default chat serializes to nothing",
      configedit.chat_lines({"permission_mode": "always-ask",
                             "system_prompt": "prompts/system.md",
                             "thought_truncation": True,
                             "compaction": {"auto": True, "threshold": 0.8}})
      == [])
lines = configedit.chat_lines({
    "provider": "ws", "model": "qwen3", "permission_mode": "allow-edits",
    "system_prompt": "prompts/mine.md", "thought_truncation": False,
    "max_output": 32768,
    "compaction": {"auto": False, "threshold": 0.5}})
cfg = libconfig.parse_text("\n".join(lines) + "\n")
check("chat serializer round-trips",
      cfg["chat"]["provider"] == "ws" and cfg["chat"]["model"] == "qwen3"
      and cfg["chat"]["permission_mode"] == "allow-edits"
      and cfg["chat"]["system_prompt"] == "prompts/mine.md"
      and cfg["chat"]["thought_truncation"] is False
      and cfg["chat"]["max_output"] == 32768
      and cfg["chat"]["compaction"] == {"auto": False, "threshold": 0.5},
      str((lines, cfg["chat"])))

eff_ask = dict(libconfig.BUILTIN_MODES["always-ask"])
check("builtin-equal modes vanish from the file",
      configedit.permission_mode_lines(
          {"always-ask": dict(eff_ask),
           "allow-edits": dict(libconfig.BUILTIN_MODES["allow-edits"])}) == [])
lines = configedit.permission_mode_lines({
    "always-ask": {**eff_ask, "shell": "deny",
                   "mcp_files_read": "ask"},        # redundant ask: dropped
    "focus": dict(eff_ask),                          # custom, no overrides
    "locked": {**eff_ask, "write_file": "disabled"},
})
text = "\n".join(lines) + "\n"
raw = yaml.safe_load(text)["permission-modes"]
check("permission serializer writes minimal diffs",
      raw == {"always-ask": {"tools": {"shell": "deny"}},
              "focus": None,
              "locked": {"tools": {"write_file": "disabled"}}},
      text)
modes = libconfig.parse_text(text)["permissionModes"]
check("permission serializer round-trips",
      modes["always-ask"]["shell"] == "deny"
      and modes["focus"] == libconfig.BUILTIN_MODES["always-ask"]
      and modes["locked"]["write_file"] == "disabled", str(modes))
try:
    configedit.permission_mode_lines({"m": {"shell": "sometimes"}})
    check("bad level raises", False)
except ValueError:
    check("bad level raises", True)

check("all-default containers serialize to nothing",
      configedit.container_lines({"engine": "auto", "default": "sandbox",
                                  "definitions": []}) == [])
lines = configedit.container_lines({
    "engine": "podman", "default": "dev",
    "definitions": [{"name": "dev", "file": "containers/dev.Containerfile"},
                    {"name": "plain"}]})
cfg = libconfig.parse_text("\n".join(lines) + "\n")
check("containers serializer round-trips",
      cfg["containers"] == {"engine": "podman", "default": "dev",
                            "definitions": [
                                {"name": "dev",
                                 "file": "containers/dev.Containerfile"},
                                {"name": "plain", "file": ""}]},
      str((lines, cfg["containers"])))

# =========================================================================
# JsApi: config_read / config_write / section + item endpoints
# =========================================================================
from loom.app import Bus, JsApi  # noqa: E402
from loom import envs as _envs  # noqa: E402

api = JsApi(Bus())
_fk = {}
_okg, _oks, _okd = _envs._kr_get, _envs._kr_set, _envs._kr_del
_envs._kr_get = lambda n: _fk.get(n)
_envs._kr_set = lambda n, v: _fk.__setitem__(n, v)
_envs._kr_del = lambda n: _fk.pop(n, None)
try:
    with tempfile.TemporaryDirectory(prefix="loomtest-cfglib-") as d:
        r = api.library_create(d + "/lib")
        check("library created for the api tests", r["ok"], str(r))
        rt = api._need_root()
        BASE = ("# hand-written header\n"
                "providers:\n"
                "- name: ws\n"
                "  vendor: llama-cpp\n"
                "  url: http://127.0.0.1:9\n"
                "\n"
                "# tail comment\n"
                "chat:\n"
                "  model: m1\n")
        (rt / "loom.yaml").write_text(BASE)

        # ---- config_read / config_write ----
        r = api.config_read()
        check("config_read serves text + mtime",
              r["ok"] and r["data"]["text"] == BASE
              and isinstance(r["data"]["mtime"], int), str(r))
        mt = r["data"]["mtime"]

        r = api.config_write("providers: {broken\n", mt)
        check("config_write rejects an unparsable candidate",
              not r["ok"] and "nothing was written" in r["error"], str(r))
        r = api.config_write("providers:\n- name: x\n  url: ftp://nope\n", mt)
        check("config_write rejects an invalid candidate",
              not r["ok"] and "nothing was written" in r["error"], str(r))
        check("a rejected save leaves the file untouched",
              (rt / "loom.yaml").read_text() == BASE)

        good = BASE.replace("model: m1", "model: m2")
        r = api.config_write(good, mt)
        check("config_write accepts a valid candidate",
              r["ok"] and (rt / "loom.yaml").read_text() == good
              and r["data"]["mtime"] >= mt, str(r))

        r = api.config_write(BASE, mt - 1)
        check("config_write catches the file changing under a dirty buffer",
              not r["ok"] and "changed on disk" in r["error"], str(r))
        check("a missing trailing newline is added",
              api.config_write("chat:\n  model: m3", None)["ok"]
              and (rt / "loom.yaml").read_text().endswith("m3\n"))
        (rt / "loom.yaml").write_text(BASE)

        # ---- provider_update / provider_remove ----
        _fk.clear()
        _fk[api._provider_key_name("ws")] = "sk-old"
        r = api.provider_update("ws", "box", "openai",
                                "http://127.0.0.1:9", "sam@box")
        text = (rt / "loom.yaml").read_text()
        provs = libconfig.load(rt)["providers"]
        check("provider_update rewrites the entry in place",
              r["ok"] and "- name: ws" not in text
              and provs == [{"name": "box", "vendor": "openai",
                             "url": "http://127.0.0.1:9",
                             "ssh": "sam@box"}], text)
        check("provider_update keeps surrounding comments",
              "# hand-written header" in text and "# tail comment" in text,
              text)
        check("a rename moves the keyring key",
              _fk.get(api._provider_key_name("box")) == "sk-old"
              and api._provider_key_name("ws") not in _fk, str(_fk))
        r = api.provider_update("box", "box", "llama-cpp",
                                "http://127.0.0.1:9", "", "sk-new")
        check("provider_update can replace the key",
              r["ok"] and _fk.get(api._provider_key_name("box")) == "sk-new",
              str((r, _fk)))
        r = api.provider_update("ghost", "ghost", "llama-cpp", "http://h:1")
        check("updating an unknown provider fails cleanly",
              not r["ok"] and "ghost" in r["error"], str(r))

        r = api.provider_remove("box")
        text = (rt / "loom.yaml").read_text()
        check("provider_remove drops the entry",
              r["ok"] and "- name: box" not in text
              and "providers:" in text and "# tail comment" in text, text)
        check("provider_remove drops the keyring key",
              api._provider_key_name("box") not in _fk, str(_fk))

        # ---- chat_config_set ----
        r = api.chat_config_set({"provider": "", "model": "qwen3",
                                 "permission_mode": "always-ask",
                                 "system_prompt": "prompts/system.md",
                                 "thought_truncation": True,
                                 "compaction": {"auto": True,
                                                "threshold": 0.8}})
        text = (rt / "loom.yaml").read_text()
        check("chat_config_set writes only non-defaults",
              r["ok"] and "chat:\n  model: qwen3\n" in text
              and "permission_mode" not in text
              and "compaction" not in text, text)
        r = api.chat_config_set({"permission_mode": "no-such-mode"})
        check("an unknown default mode is rejected before writing",
              not r["ok"] and "no-such-mode" in r["error"]
              and "no-such-mode" not in (rt / "loom.yaml").read_text(),
              str(r))

        # ---- permission_modes_set (incl. legacy removal) ----
        (rt / "loom.yaml").write_text(
            BASE + "permissions:\n  tools:\n    shell: deny\n")
        eff = {n: dict(t) for n, t in libconfig.BUILTIN_MODES.items()}
        eff["always-ask"]["shell"] = "deny"
        eff["focus"] = dict(libconfig.BUILTIN_MODES["always-ask"])
        r = api.permission_modes_set(eff)
        text = (rt / "loom.yaml").read_text()
        check("permission_modes_set writes diffs + drops the legacy block",
              r["ok"] and "permissions:\n" not in text
              and "permission-modes:" in text and "focus:" in text
              and "shell: deny" in text, text)
        modes = libconfig.load(rt)["permissionModes"]
        check("the saved modes load back",
              modes["always-ask"]["shell"] == "deny" and "focus" in modes,
              str(modes))
        eff["always-ask"]["shell"] = "ask"      # back to the builtin default
        del eff["focus"]
        r = api.permission_modes_set(eff)
        text = (rt / "loom.yaml").read_text()
        check("all-builtin modes remove the section entirely",
              r["ok"] and "permission-modes" not in text, text)

        # ---- containers_config_set ----
        r = api.containers_config_set({"engine": "docker", "default": "dev",
                                       "definitions": [{"name": "dev",
                                                        "file": "c/dev"}]})
        text = (rt / "loom.yaml").read_text()
        check("containers_config_set writes the block",
              r["ok"] and "containers:\n  engine: docker" in text
              and "- name: dev" in text, text)
        r = api.containers_config_set({"engine": "auto", "default": "",
                                       "definitions": []})
        check("an all-default containers block is removed",
              r["ok"] and "containers" not in (rt / "loom.yaml").read_text())

        # ---- mcp_update / mcp_remove ----
        (rt / "loom.yaml").write_text(
            BASE + "mcp-servers:\n- name: files\n  command: fs-server /tmp\n")
        r = api.mcp_update("files", "docs", "fs-server /docs", {"K": "v"})
        text = (rt / "loom.yaml").read_text()
        check("mcp_update rewrites the entry",
              r["ok"] and "- name: docs" in text
              and "command: fs-server /docs" in text and "K: v" in text
              and "- name: files" not in text, text)
        check("mcp_update returns status rows",
              r["data"]["servers"][0]["name"] == "docs", str(r))
        r = api.mcp_remove("docs")
        text = (rt / "loom.yaml").read_text()
        check("mcp_remove drops the entry",
              r["ok"] and "- name: docs" not in text
              and r["data"]["servers"] == [], text)
        check("mcp comments elsewhere survived the round trip",
              "# hand-written header" in text and "# tail comment" in text,
              text)
finally:
    _envs._kr_get, _envs._kr_set, _envs._kr_del = _okg, _oks, _okd

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
