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

# ---------- split-gguf shard grouping (scan) ----------
out = (f"100\t{FAKE_HOME}/models/big/Model-Q8_0-00002-of-00003.gguf\n"
       f"100\t{FAKE_HOME}/models/big/Model-Q8_0-00001-of-00003.gguf\n"
       f"50\t{FAKE_HOME}/models/big/Model-Q8_0-00003-of-00003.gguf\n"
       f"60\t{FAKE_HOME}/models/big/mmproj-Model-F16.gguf\n"
       f"70\t{FAKE_HOME}/models/big/whole.gguf\n")
entries = models.parse_scan(out, FAKE_HOME)
split = next(e for e in entries if e.get("parts"))
check("scan collapses shards to one entry",
      len([e for e in entries if "Q8_0" in e["name"]]) == 1, str(entries))
check("scan split entry points at shard 1",
      split["path"] == "~/models/big/Model-Q8_0-00001-of-00003.gguf", str(split))
check("scan split entry sums sizes and collapses the name",
      split["size"] == 250 and split["name"] == "Model-Q8_0.gguf", str(split))
check("scan split entry lists shards in order",
      split["parts"] == [f"~/models/big/Model-Q8_0-0000{i}-of-00003.gguf"
                         for i in (1, 2, 3)], str(split))
check("mmproj pairs to the grouped split entry",
      next(e for e in entries if e["mmproj"])["pairedWith"] == split["path"])
check("whole file in same dir stays its own entry",
      any(e["name"] == "whole.gguf" and "parts" not in e for e in entries))

# ---------- split-gguf shard grouping (HF repo listing) ----------
raw = [{"path": f"Q8_0/M-Q8_0-0000{i}-of-00002.gguf", "size": 10 * i,
        "url": f"https://x/Q8_0/M-Q8_0-0000{i}-of-00002.gguf"}
       for i in (2, 1)]
raw.append({"path": "mmproj-F16.gguf", "size": 5, "url": "https://x/mm"})
grouped = models.group_split(raw)
q = next(f for f in grouped if f.get("parts"))
check("repo grouping: one entry per quant, display path collapsed",
      len(grouped) == 2 and q["path"] == "Q8_0/M-Q8_0.gguf", str(grouped))
check("repo grouping: url is shard 1, size is the total",
      q["url"].endswith("00001-of-00002.gguf") and q["size"] == 30, str(q))
check("repo grouping: parts ordered",
      [p["path"] for p in q["parts"]]
      == [f"Q8_0/M-Q8_0-0000{i}-of-00002.gguf" for i in (1, 2)], str(q))
check("repo grouping: single files untouched",
      any(f["path"] == "mmproj-F16.gguf" and "parts" not in f for f in grouped))

# ---------- live local scan ----------
d = Path(FAKE_HOME) / "models"
d.mkdir(parents=True, exist_ok=True)
(d / "tiny.gguf").write_bytes(b"GGUF" + b"x" * 100)
(d / "mmproj-tiny.gguf").write_bytes(b"GGUF" + b"y" * 50)
(d / "not-a-model.bin").write_bytes(b"nope")
# the HF hub layout: bytes live in an extensionless blob, the *.gguf name
# is a SYMLINK in snapshots/ — the scan must dereference it
hub = Path(FAKE_HOME) / ".cache/huggingface/hub/models--org--repo"
(hub / "blobs").mkdir(parents=True, exist_ok=True)
(hub / "snapshots/abc").mkdir(parents=True, exist_ok=True)
(hub / "blobs/deadbeef").write_bytes(b"GGUF" + b"h" * 500)
(hub / "snapshots/abc/hub-model.gguf").symlink_to(hub / "blobs/deadbeef")
found = models.scan("")
names = {e["name"] for e in found}
check("local scan finds ggufs", {"tiny.gguf", "mmproj-tiny.gguf"} <= names, str(names))
check("local scan ignores non-gguf", "not-a-model.bin" not in names)
hubhit = next((e for e in found if e["name"] == "hub-model.gguf"), None)
check("scan finds HF-hub symlinked ggufs with the real size",
      hubhit is not None and hubhit["size"] == 504, str(hubhit))
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
snip_split = models.yaml_snippet(
    [{"path": "~/m/Qwen3-27B-Q4_K_M-00001-of-00005.gguf", "context": 4096}])
check("display name drops the shard suffix",
      "name: Qwen3 27B\n" in snip_split, snip_split)

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

# ---------- downloads: a local HTTP server with Range support ----------
PAYLOAD = b"GGUF" + os.urandom(2_000_000)
RANGES = []          # every Range start the server honored


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if "missing" in self.path:
            self.send_error(404)
            return
        start = 0
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            RANGES.append(start)
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            self.send_response(200)
        body = PAYLOAD[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if "slow" in self.path:  # trickle so pause can land mid-file
                for i in range(0, len(body), 100_000):
                    self.wfile.write(body[i:i + 100_000])
                    self.wfile.flush()
                    time.sleep(0.05)
            else:
                self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass                     # a paused client hung up mid-body


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]

events = []
done = threading.Event()


def push(ev):
    events.append(ev)
    if ev.get("kind") in ("done", "error", "paused", "cancelled"):
        done.set()


models.start_download(push, "job1", f"http://127.0.0.1:{port}/dl/test-model.gguf")
check("download finished", done.wait(20) and events[-1]["kind"] == "done",
      str(events[-1:]))
dest = models.local_models_dir() / "test-model.gguf"
check("download landed in ~/.loom/models",
      dest.is_file() and dest.read_bytes() == PAYLOAD)
rec = next((r for r in models.downloads() if r["id"] == "job1"), None)
check("finished download has a done record",
      rec is not None and rec["status"] == "done", str(rec))
models.dismiss_download("job1")
check("dismiss drops the record, keeps the file",
      not any(r["id"] == "job1" for r in models.downloads()) and dest.is_file())
try:
    models.start_download(push, "job2", f"http://127.0.0.1:{port}/dl/test-model.gguf")
    check("duplicate download refused", False)
except models.ModelsError:
    check("duplicate download refused", True)

# ---------- multi-shard (split gguf) download ----------
shard_urls = [f"http://127.0.0.1:{port}/dl/Split-Q8_0-0000{i}-of-00003.gguf"
              for i in (1, 2, 3)]
events.clear()
done.clear()
models.start_download(push, "job3", shard_urls, total=3 * len(PAYLOAD))
check("split download finished", done.wait(20) and events[-1]["kind"] == "done",
      str(events[-1:]))
ev = events[-1]
check("split done event: collapsed name, shard-1 path, parts count",
      ev.get("name") == "Split-Q8_0.gguf" and ev.get("parts") == 3
      and ev.get("path", "").endswith("Split-Q8_0-00001-of-00003.gguf"), str(ev))
shards = [models.local_models_dir() / f"Split-Q8_0-0000{i}-of-00003.gguf"
          for i in (1, 2, 3)]
check("all shards landed with their exact names",
      all(p.is_file() and p.read_bytes() == PAYLOAD for p in shards))

# ---------- a failing shard keeps its bytes (resumable), cancel wipes ----------
events.clear()
done.clear()
models.start_download(push, "job4", [
    f"http://127.0.0.1:{port}/dl/Half-Q4-00001-of-00002.gguf",
    f"http://127.0.0.1:{port}/dl/missing-Half-Q4-00002-of-00002.gguf"])
check("failing shard errors the job",
      done.wait(20) and events[-1]["kind"] == "error", str(events[-1:]))
rec = next((r for r in models.downloads() if r["id"] == "job4"), None)
check("failed download keeps an error record with its bytes",
      rec is not None and rec["status"] == "error"
      and rec["done"] == len(PAYLOAD), str(rec))
check("finished shard survives the failure",
      (models.local_models_dir() / "Half-Q4-00001-of-00002.gguf").is_file())
try:
    models.start_download(push, "job4b",
                          f"http://127.0.0.1:{port}/dl/Half-Q4-00001-of-00002.gguf")
    check("name held by an unfinished record refused", False)
except models.ModelsError:
    check("name held by an unfinished record refused", True)
models.cancel_download("job4")
check("cancel wipes the record and its bytes",
      not any(r["id"] == "job4" for r in models.downloads())
      and not list(models.local_models_dir().glob("Half-Q4*")))

# ---------- pause mid-flight, resume via HTTP Range ----------
events.clear()
done.clear()
models.start_download(push, "job5", f"http://127.0.0.1:{port}/slow/paused.gguf")
for _ in range(200):                     # wait for the first progress event
    if any(e["kind"] == "progress" for e in events):
        break
    time.sleep(0.05)
check("pause reaches the live worker", models.pause_download("job5"))
check("worker reports paused", done.wait(10) and events[-1]["kind"] == "paused",
      str(events[-1:]))
part = models.local_models_dir() / "paused.gguf.part"
rec = next((r for r in models.downloads() if r["id"] == "job5"), None)
check("paused download keeps its .part and record",
      part.is_file() and 0 < part.stat().st_size < len(PAYLOAD)
      and rec is not None and rec["status"] == "paused"
      and rec["done"] == part.stat().st_size, str(rec))
RANGES.clear()
events.clear()
done.clear()
models.resume_download(push, "job5")
check("resume finishes the download",
      done.wait(20) and events[-1]["kind"] == "done", str(events[-1:]))
final = models.local_models_dir() / "paused.gguf"
check("resumed file is byte-identical",
      final.is_file() and final.read_bytes() == PAYLOAD)
check("resume used a Range request from the .part offset",
      len(RANGES) == 1 and RANGES[0] > 0, str(RANGES))
check("pause on a dead job is a no-op", models.pause_download("job5") is False)

# ---------- crash recovery: stale 'active' records surface as paused ----------
from loom import store
(models.local_models_dir() / "crashed.gguf.part").write_bytes(PAYLOAD[:1234])
store.mutate_state(lambda st: st.__setitem__("downloads", st.get("downloads", []) + [
    {"id": "job6", "urls": [f"http://127.0.0.1:{port}/dl/crashed.gguf"],
     "names": ["crashed.gguf"], "label": "crashed.gguf",
     "total": len(PAYLOAD), "done": 99, "status": "active", "error": "",
     "ts": 0}]))
rec = next((r for r in models.downloads() if r["id"] == "job6"), None)
check("interrupted record surfaces as paused, bytes recounted from disk",
      rec is not None and rec["status"] == "paused" and rec["done"] == 1234,
      str(rec))
events.clear()
done.clear()
models.resume_download(push, "job6")
check("interrupted record resumes to completion",
      done.wait(20) and events[-1]["kind"] == "done"
      and (models.local_models_dir() / "crashed.gguf").read_bytes() == PAYLOAD,
      str(events[-1:]))
httpd.shutdown()

# ---------- history: done records stay until cleared; clear spares live ----------
recs = models.downloads()
check("finished downloads are retained as history",
      {r["id"] for r in recs if r["status"] == "done"}
      >= {"job3", "job5", "job6"}, str(recs))
check("done records carry a completion timestamp",
      all(r.get("doneTs") for r in recs if r["status"] == "done"), str(recs))
store.mutate_state(lambda st: st.__setitem__(
    "downloads", st.get("downloads", []) + [
        {"id": "job7", "urls": ["http://x/a.gguf"], "names": ["a.gguf"],
         "label": "a.gguf", "total": 1, "done": 0, "status": "paused",
         "error": "", "ts": 0}]))
models.clear_downloads()
recs = models.downloads()
check("clear removes all history, keeps unfinished records",
      [r["id"] for r in recs] == ["job7"], str(recs))
check("cleared history leaves the files alone",
      (models.local_models_dir() / "crashed.gguf").is_file()
      and all(p.is_file() for p in shards))
models.cancel_download("job7")

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
