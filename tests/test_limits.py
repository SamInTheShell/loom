"""Per-host server concurrency: store limits + srv.make_room.

Run: uv run python tests/test_limits.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-home-")
os.environ["HOME"] = tempfile.mkdtemp(prefix="loomtest-userhome-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import srv, store  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" — {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- store: limits ----------
check("default limit is 1", store.server_limit("") == 1)
check("default limit is 1 for ssh hosts", store.server_limit("user@gpu") == 1)
store.set_server_limit("", 3)
store.set_server_limit("user@gpu", -1)
check("limit persisted", store.server_limit("") == 3)
check("-1 means no limit", store.server_limit("user@gpu") == -1)
check("limits map", store.server_limits() == {"": 3, "user@gpu": -1})
for bad in (0, -2, 99):
    try:
        store.set_server_limit("", bad)
        check(f"limit {bad} rejected", False)
    except ValueError:
        check(f"limit {bad} rejected", True)
store.set_server_limit("", 1)

# ---------- srv.make_room ----------
def seed(states):
    with srv._lock:
        srv._servers.clear()
        for sid, (host, state, ts) in states.items():
            srv._servers[sid] = {"id": sid, "host": host, "state": state,
                                 "ts": ts, "name": sid.upper()}


notes = []
seed({"a": ("", "running", 1), "b": ("", "running", 2),
      "c": ("gpu", "running", 3), "d": ("", "stopped", 4)})
stopped = srv.make_room("", "new", 1, notice=notes.append)
check("limit 1 stops every other live server on the host",
      stopped == ["a", "b"], str(stopped))
check("other hosts untouched, stopped servers ignored",
      srv._servers["c"]["state"] == "running", str(srv._servers.get("c")))
check("a notice per stop", len(notes) == 2 and "A" in notes[0], str(notes))

seed({"a": ("", "running", 2), "b": ("", "loading", 1)})
stopped = srv.make_room("", "new", 2)
check("limit 2 with two live stops only the oldest",
      stopped == ["b"], str(stopped))

seed({"a": ("", "running", 1)})
check("no limit stops nothing", srv.make_room("", "new", -1) == [])
check("the server being started never stops itself",
      srv.make_room("", "a", 1) == [])
seed({"a": ("", "running", 1)})
check("room already available → nothing stops",
      srv.make_room("", "new", 2) == [])

# ---------- container: run llama-server inside podman/docker ----------
REC = {"name": "V", "model": "~/.lmstudio/models/q/x.gguf",
       "mmproj": "~/.lmstudio/models/q/mm.gguf", "ctx": 4096,
       "flags": "-ngl 99",
       "container": "podman run --rm --device /dev/dri llama-vk:latest"}
binp, prelude, spawn = srv._spawn_parts(REC, srv.compose_args(REC))
check("container mode probes the engine", binp == "podman", binp)
check("socket dir always mounted",
      'LOOM_MNT=(-v "${S%/*}":"${S%/*}")' in prelude, prelude)
check("model dir mount is CONDITIONAL on host existence",
      '[ -d "$HOME"/.lmstudio/models/q ] && LOOM_MNT+=(-v '
      '"$HOME"/.lmstudio/models/q:"$HOME"/.lmstudio/models/q)' in prelude,
      prelude)
check("mmproj shares the mount (same dir, once)",
      prelude.count("LOOM_MNT+=") == 1, prelude)
check("mounts land between run and the image",
      spawn.index(" run ") < spawn.index('"${LOOM_MNT[@]}"')
      < spawn.index("llama-vk:latest"), spawn)
check("socket + args ride after the image",
      spawn.index("llama-vk:latest") < spawn.index('--host "$S"')
      < spawn.index("-ngl 99"), spawn)
check("plain entries keep the binary path",
      srv._spawn_parts({"name": "P", "model": "/m.gguf", "ctx": 0,
                        "flags": ""}, ["-m", "/m.gguf"])[0] == "llama-server")
REC_MD = dict(REC, flags="-md ~/.cache/huggingface/hub/snap/draft.gguf\n-ngl 99")
_b, prelude_md, _s = srv._spawn_parts(REC_MD, srv.compose_args(REC_MD))
check("draft-model dir from flags gets a conditional mount too",
      '[ -d "$HOME"/.cache/huggingface/hub/snap ]' in prelude_md, prelude_md)
for bad in ("podman", "echo hi there", "podman run -it img",
            "podman run --rm -v ~/.cache:/root/.cache"):
    try:
        srv._spawn_parts(dict(REC, container=bad), ["-m", "x"])
        check(f"container {bad!r} rejected", False)
    except srv.SrvError:
        check(f"container {bad!r} rejected", True)
check("spawn preview substitutes placeholders",
      "<socket>" in srv.spawn_preview(REC)
      and "<socket dir>" in srv.spawn_preview(REC))

# end-to-end: a fake podman shows exactly what the engine receives — host
# paths get auto-mounted, container-internal paths (/root/…) do NOT
import subprocess
fake = Path(tempfile.mkdtemp(prefix="loomtest-fakebin-"))
(fake / "podman").write_text('#!/bin/bash\nfor a in "$@"; do echo "A:$a"; done\n')
os.chmod(fake / "podman", 0o755)
(Path(os.environ["HOME"]) / ".lmstudio/models/q").mkdir(parents=True,
                                                        exist_ok=True)
MIXED = {"name": "V", "model": "~/.lmstudio/models/q/x.gguf",
         "mmproj": "", "ctx": 0,
         "flags": "-md /root/.cache/huggingface/hub/s/d.gguf",
         "container": "podman run --rm -v ~/.cache:/root/.cache img:1"}
_b2, pre2, spawn2 = srv._spawn_parts(MIXED, srv.compose_args(MIXED))
script = ('S=/tmp/x.sock\nexport PATH=' + str(fake) + ':"$PATH"\n'
          + pre2 + spawn2 + "\n")
out = subprocess.run(["bash", "-s"], input=script, capture_output=True,
                     text=True).stdout
home = os.environ["HOME"]
check("existing host dir auto-mounted at an identical path",
      f"A:{home}/.lmstudio/models/q:{home}/.lmstudio/models/q" in out, out)
check("container-internal /root path NOT auto-mounted",
      "A:/root/.cache/huggingface/hub/s:/root" not in out, out)
check("the user's own -v mapping passes through, source ~ expanded",
      f"A:{home}/.cache:/root/.cache" in out, out)
check("-md arg passes through verbatim for the user's mapping",
      "A:/root/.cache/huggingface/hub/s/d.gguf" in out, out)

print()
if FAILS:
    print("FAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
