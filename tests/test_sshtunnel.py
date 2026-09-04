"""sshtunnel plumbing tests with a STUB ssh binary - no sshd needed.

The stub swallows ssh's option flags and executes the remote command
locally with bash, so the stdio↔TCP bridge really runs and the whole
request path (channel, socketpair pumps, http.client, response teardown)
is exercised end to end. Also vets the KEYS-ONLY policy surface: the
argv must carry BatchMode=yes, and an auth failure's stderr hint gains
the actionable ssh-agent sentence.

Run: uv run python tests/test_sshtunnel.py
"""

import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import sshtunnel  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}" + (f" - {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- a local HTTP target ----------
class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]

# ---------- direct (no ssh) ----------
resp = sshtunnel.request("GET", f"http://127.0.0.1:{port}/direct",
                         None, None, 10, "")
check("direct TCP request works",
      json.loads(resp.read())["path"] == "/direct")
resp.close()

try:
    sshtunnel.request("GET", "http://127.0.0.1:1/nope", None, None, 3, "")
    check("direct connection-refused raises URLError", False)
except urllib.error.URLError:
    check("direct connection-refused raises URLError", True)

# ---------- the stub ssh: run the remote command locally ----------
stub = Path(tempfile.mkdtemp(prefix="loomtest-ssh-")) / "fakessh"
stub.write_text(
    "#!/bin/bash\n"
    "# swallow ssh options (-o v ...), take <host> <command>\n"
    'args=(); while [ $# -gt 0 ]; do\n'
    '  case "$1" in -o) shift 2;; *) args+=("$1"); shift;; esac\n'
    "done\n"
    '# args[0]=host, the rest is the command\n'
    'echo "${args[0]}" >> "$FAKESSH_LOG"\n'
    'exec bash -c "${args[*]:1}"\n')
stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
log = Path(tempfile.mkdtemp(prefix="loomtest-sshlog-")) / "hosts"
os.environ["FAKESSH_LOG"] = str(log)

old_cmd = sshtunnel.SSH_CMD
sshtunnel.SSH_CMD = [str(stub)]
try:
    resp = sshtunnel.request("GET", f"http://127.0.0.1:{port}/tunneled",
                             None, None, 15, "user@remotebox")
    check("tunneled request rides the ssh stdio bridge",
          json.loads(resp.read())["path"] == "/tunneled")
    resp.close()
    check("the stub saw the destination",
          "user@remotebox" in log.read_text())

    # keys-only policy: the real argv must force BatchMode
    check("argv forces BatchMode (no password prompts, ever)",
          "BatchMode=yes" in " ".join(
              a for pair in zip(*[iter(sshtunnel._mux_args())] * 2)
              for a in pair))

    # host-shaped-like-a-flag is refused outright (argv injection)
    try:
        sshtunnel.request("GET", f"http://127.0.0.1:{port}/x",
                          None, None, 5, "-oProxyCommand=evil")
        check("flag-shaped ssh host refused", False)
    except urllib.error.URLError as e:
        check("flag-shaped ssh host refused", "invalid SSH host" in str(e.reason))

    # auth-refused stderr gains the actionable keys-only hint
    denied = Path(str(stub) + "-denied")
    denied.write_text("#!/bin/bash\n"
                      "echo 'user@host: Permission denied (publickey,password).' >&2\n"
                      "exit 255\n")
    denied.chmod(denied.stat().st_mode | stat.S_IEXEC)
    sshtunnel.SSH_CMD = [str(denied)]
    try:
        sshtunnel.request("GET", f"http://127.0.0.1:{port}/x",
                          None, None, 5, "user@locked")
        check("auth failure raises", False)
    except urllib.error.URLError as e:
        msg = str(e.reason)
        check("auth failure raises", True)
        check("denied hint tells the user about ssh-agent",
              "Permission denied" in msg and "ssh-agent" in msg, msg)
finally:
    sshtunnel.SSH_CMD = old_cmd

# https over the tunnel is refused (the ssh channel is the crypto layer)
try:
    sshtunnel.request("GET", "https://example.com/", None, None, 5, "host")
    check("https over the tunnel refused", False)
except urllib.error.URLError as e:
    check("https over the tunnel refused",
          "not supported" in str(e.reason), str(e.reason))

httpd.shutdown()
print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
