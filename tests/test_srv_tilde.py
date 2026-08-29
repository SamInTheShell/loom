"""Tilde expansion in server paths. A shell expands ~ before llama-server
sees it; Loom's quoted supervisor-script args skip that, so srv._q1 must
reproduce it host-side ("$HOME"'/rest'). Covers: the quoting shape, real
bash expansion semantics, and a FULL srv.start() spawn of a fake
llama-server that records its argv and serves /health on the unix socket.

Run: uv run python tests/test_srv_tilde.py
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
FAKE_HOME = tempfile.mkdtemp(prefix="loomtest-userhome-")
os.environ["HOME"] = FAKE_HOME   # the supervisor bash inherits this $HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import srv  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}" + (f" — {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- quoting shape ----------
check("plain args still shell-quoted", srv._q(["-m", "a b.gguf"]) == "-m 'a b.gguf'")
check("tilde path becomes $HOME", srv._q1("~/m.gguf") == '"$HOME"/m.gguf'
      or srv._q1("~/m.gguf") == '"$HOME"' + "'/m.gguf'"
      or srv._q1("~/m.gguf").startswith('"$HOME"'))
check("bare tilde", srv._q1("~") == '"$HOME"')
check("mid-string tilde untouched", srv._q1("a~b") == "'a~b'" or srv._q1("a~b") == "a~b")

# ---------- bash agrees ----------
out = subprocess.run(
    ["bash", "-c", "printf '%s\\n' " + srv._q(["-m", "~/models/a b.gguf", "-c", "128000"])],
    capture_output=True, text=True, env={**os.environ, "HOME": FAKE_HOME})
lines = out.stdout.splitlines()
check("bash expands the tilde arg",
      lines == ["-m", f"{FAKE_HOME}/models/a b.gguf", "-c", "128000"], str(lines))

# ---------- full spawn through the supervisor ----------
# a fake llama-server: records argv, then serves /health on the unix socket
bindir = Path(tempfile.mkdtemp(prefix="loomtest-bin-"))
fake = bindir / "llama-server"
fake.write_text(f"""#!/usr/bin/env python3
import json, os, sys, socketserver
from http.server import BaseHTTPRequestHandler
args = sys.argv[1:]
open(os.path.join({str(FAKE_HOME)!r}, "argv.json"), "w").write(json.dumps(args))
sock = args[args.index("--host") + 1]
class H(BaseHTTPRequestHandler):
    def address_string(self): return "unix"
    def log_message(self, *a): pass
    def do_GET(self):
        body = b'{{"status":"ok"}}' if self.path == "/health" else b'{{}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
socketserver.ThreadingUnixStreamServer(sock, H).serve_forever()
""")
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"

model_dir = Path(FAKE_HOME) / "fake-models"
model_dir.mkdir()
(model_dir / "m.gguf").write_bytes(b"GGUF fake")
(model_dir / "mm.gguf").write_bytes(b"GGUF fake mmproj")

rec = {"id": "tilde-test-1", "name": "tilde test", "host": "",
       "model": "~/fake-models/m.gguf", "mmproj": "~/fake-models/mm.gguf",
       "ctx": 4096, "flags": "-np 1"}
try:
    got = srv.start(rec, load_timeout=30)
    check("server started and answered /health", got.get("started") is True, str(got))
    argv = json.loads((Path(FAKE_HOME) / "argv.json").read_text())
    check("no literal tilde reached the server",
          not any(a.startswith("~") for a in argv), str(argv))
    check("model path expanded to $HOME",
          f"{FAKE_HOME}/fake-models/m.gguf" in argv, str(argv))
    check("mmproj path expanded to $HOME",
          f"{FAKE_HOME}/fake-models/mm.gguf" in argv, str(argv))
    check("alias passed through", "tilde test" in argv, str(argv))
    srv.stop("", rec["id"])
    check("stopped clean", srv.state_of(rec["id"]) == "stopped")
except srv.SrvError as e:
    check("server started and answered /health", False, str(e))
finally:
    srv.shutdown()

# ---------- probe_host with a tilde model path ----------
probe = srv.probe_host("", "llama-server", "~/fake-models/m.gguf")
check("probe sees tilde model file", probe.get("model") is True, str(probe))
probe = srv.probe_host("", "llama-server", "~/fake-models/missing.gguf")
check("probe honest about a missing file", probe.get("model") is False, str(probe))

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
