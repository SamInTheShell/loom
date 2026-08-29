"""The models utility: scan parsing + mmproj pairing, live local scan,
yaml snippet generation (round-tripped through libconfig), host config,
and a real download from a local HTTP server.

Run: uv run python tests/test_models.py
"""

import http.server
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
FAKE_HOME = tempfile.mkdtemp(prefix="loomtest-userhome-")
os.environ["HOME"] = FAKE_HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import libconfig, library, models  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}" + (f" — {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- parse_scan + mmproj pairing ----------
out = (f"5000000000\t{FAKE_HOME}/.lmstudio/models/qwen/Qwen3-27B-Q4_K_M.gguf\n"
       f"800000000\t{FAKE_HOME}/.lmstudio/models/qwen/mmproj-Qwen3-BF16.gguf\n"
       f"2000000000\t{FAKE_HOME}/models/other.gguf\n")
entries = models.parse_scan(out, FAKE_HOME)
check("tilde collapse", all(e["path"].startswith("~/") for e in entries),
      str(entries))
mm = next(e for e in entries if e["mmproj"])
main = next(e for e in entries if e["name"].startswith("Qwen3-27B"))
check("mmproj flagged", mm["name"].startswith("mmproj"))
check("mmproj paired to the big sibling", mm["pairedWith"] == main["path"],
      str(mm))
check("unpaired dir has no mmproj",
      next(e for e in entries if e["name"] == "other.gguf")["pairedWith"] is None)

# ---------- live local scan ----------
d = Path(FAKE_HOME) / "models"
d.mkdir(parents=True, exist_ok=True)
(d / "tiny.gguf").write_bytes(b"GGUF" + b"x" * 100)
(d / "mmproj-tiny.gguf").write_bytes(b"GGUF" + b"y" * 50)
(d / "not-a-model.bin").write_bytes(b"nope")
found = models.scan("")
names = {e["name"] for e in found}
check("local scan finds ggufs", {"tiny.gguf", "mmproj-tiny.gguf"} <= names, str(names))
check("local scan ignores non-gguf", "not-a-model.bin" not in names)
check("local scan pairs mmproj",
      next(e for e in found if e["name"] == "mmproj-tiny.gguf")["pairedWith"]
      == "~/models/tiny.gguf", str(found))

# ---------- yaml snippet → valid loom.yaml ----------
snippet = models.yaml_snippet([
    {"path": "~/models/tiny.gguf", "mmproj": "~/models/mmproj-tiny.gguf",
     "host": "gpubox", "context": 65536},
    {"path": "~/.lmstudio/models/qwen/Qwen3-27B-Q4_K_M.gguf"},
])
check("snippet starts with models:", snippet.startswith("models:"), snippet)
check("snippet has literal flags block", "flags: |" in snippet)
with tempfile.TemporaryDirectory() as td:
    root = library.create_library(td + "/lib")
    (root / "loom.yaml").write_text(snippet)
    cfg = libconfig.load(root)
    check("snippet parses as loom.yaml", len(cfg["models"]) == 2, snippet)
    m0 = cfg["models"][0]
    check("snippet carries ssh + mmproj + context",
          m0["host"] == "gpubox" and m0["mmproj"] == "~/models/mmproj-tiny.gguf"
          and m0["ctx"] == 65536, str(m0))
    check("display name cleaned",
          cfg["models"][1]["name"] == "Qwen3 27B", cfg["models"][1]["name"])
    from loom import srv
    check("snippet composes llama args", "--mmproj" in srv.compose_args(m0))

# ---------- non-destructive loom.yaml injection ----------
ENTRY = {"path": "~/models/tiny.gguf", "name": "Tiny", "context": 8192,
         "mmproj": "~/models/mmproj-tiny.gguf",
         "flags": ["# backend: vulkan", "-ngl 99", "-fa on"],
         "binary": "llama-server"}

with tempfile.TemporaryDirectory() as td:
    root = library.create_library(td + "/lib")
    orig = (root / "loom.yaml").read_text()
    new = models.inject_model(orig, ENTRY)
    (root / "loom.yaml").write_text(new)
    cfg = libconfig.load(root)
    check("inject into template parses",
          len(cfg["models"]) == 1 and cfg["models"][0]["name"] == "Tiny"
          and cfg["models"][0]["mmproj"] == "~/models/mmproj-tiny.gguf")
    check("inject keeps every original comment",
          all(ln in new for ln in orig.splitlines() if ln.lstrip().startswith("#")),
          "missing comments")
    check("inject keeps section order",
          new.index("models:") < new.index("chat:") < new.index("permission-modes:")
          < new.index("containers:"))
    check("backend comment in flags",
          "# backend: vulkan" in cfg["models"][0]["flags"])

    # second injection appends at the END of the models block
    new2 = models.inject_model(new, {"path": "~/m2.gguf", "name": "Second",
                                     "context": 4096})
    (root / "loom.yaml").write_text(new2)
    cfg = libconfig.load(root)
    check("second entry appends after the first",
          [m["name"] for m in cfg["models"]] == ["Tiny", "Second"])

# missing models: key → appended block
no_key = "chat:\n  model: ''\n# tail comment\n"
new3 = models.inject_model(no_key, {"path": "/x.gguf", "name": "X", "context": 2048})
check("missing models key appends a block",
      "models:" in new3 and "# tail comment" in new3
      and new3.index("chat:") < new3.index("models:"), new3)
try:
    models.inject_model("models: []\n", {"name": "nopath"})
    check("entry without path rejected", False)
except models.ModelsError:
    check("entry without path rejected", True)

# ---------- gguf header metadata ----------
import struct
def _kv_str(s):
    b = s.encode()
    return struct.pack("<Q", len(b)) + b
gguf = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
        + struct.pack("<Q", 2)
        + _kv_str("llama.context_length") + struct.pack("<I", 4) + struct.pack("<I", 8192)
        + _kv_str("general.name") + struct.pack("<I", 8) + _kv_str("Tiny Llama"))
mp = Path(FAKE_HOME) / "models" / "meta.gguf"
mp.write_bytes(gguf)
meta = models.gguf_meta(str(mp))
check("gguf meta reads trained context",
      meta.get("ctx") == 8192 and meta.get("name") == "Tiny Llama", str(meta))
check("gguf meta on garbage is empty",
      models.gguf_meta(str(Path(FAKE_HOME) / "models" / "tiny.gguf")) == {})

# ---------- edited-snippet injection (the wizard's final step) ----------
snip = models.yaml_snippet([{"path": "~/m.gguf", "name": "Edited",
                             "context": 4096}])
check("generated flags carry the full set",
      "--spec-type draft-mtp" in snip and "-ctk q4_0 -ctv q4_0" in snip
      and "-np 1" in snip, snip)
edited = snip.replace("-ngl 99", "-ngl 45  # my tweak")
lines = models.snippet_entry_lines(edited)
check("edited snippet keeps user formatting",
      any("# my tweak" in ln for ln in lines))
import yaml as _y
newt = models.inject_lines("models: []\n# keep me\nchat:\n  model: ''\n", lines)
parsed = _y.safe_load(newt)
check("edited lines inject and parse",
      parsed["models"][0]["name"] == "Edited"
      and "# my tweak" in newt and "# keep me" in newt, newt)
for bad in ("models: []", "not: [valid", "just text"):
    try:
        models.snippet_entry_lines(bad)
        check("bad snippet rejected: " + bad[:12], False)
    except models.ModelsError:
        check("bad snippet rejected: " + bad[:12], True)

# ---------- HF repo spec parsing ----------
for spec in ("unsloth/Qwen3.8-27B-GGUF",
             "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF",
             "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/tree/main",
             "huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/x.gguf".replace(
                 "huggingface.co", "https://huggingface.co")):
    check("parse_repo: " + spec[:44],
          models.parse_repo(spec) == "unsloth/Qwen3.8-27B-GGUF")
for bad in ("", "justonename", "https://example.com/a/b", "api/models/x"):
    try:
        models.parse_repo(bad)
        check("parse_repo rejects " + repr(bad), False)
    except models.ModelsError:
        check("parse_repo rejects " + repr(bad), True)

# ---------- hosts ----------
check("no hosts yet", models.hosts() == [])
models.add_host("user@gpubox")
models.add_host("othernode")
models.add_host("user@gpubox")   # dedupe
check("hosts persisted", models.hosts() == ["user@gpubox", "othernode"])
models.remove_host("user@gpubox")
check("host removed", models.hosts() == ["othernode"])
try:
    models.add_host("-oProxyCommand=evil")
    check("flag-like host rejected", False)
except models.ModelsError:
    check("flag-like host rejected", True)

# ---------- download from a local HTTP server ----------
PAYLOAD = b"GGUF" + os.urandom(200_000)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]

events = []
done = threading.Event()


def push(ev):
    events.append(ev)
    if ev.get("kind") in ("done", "error"):
        done.set()


models.start_download(push, "job1", f"http://127.0.0.1:{port}/dl/test-model.gguf")
check("download finished", done.wait(20) and events[-1]["kind"] == "done",
      str(events[-1:]))
dest = models.local_models_dir() / "test-model.gguf"
check("download landed in ~/.loom/models",
      dest.is_file() and dest.read_bytes() == PAYLOAD)
try:
    models.start_download(push, "job2", f"http://127.0.0.1:{port}/dl/test-model.gguf")
    check("duplicate download refused", False)
except models.ModelsError:
    check("duplicate download refused", True)
httpd.shutdown()

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
