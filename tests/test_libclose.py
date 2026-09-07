"""Library-close teardown ordering: a dying chat worker's final save
must land BEFORE the library lock releases - and a wedged worker must
not hold the switch hostage past the bounded join budget. Script-style:
run with `uv run python tests/test_libclose.py`; nonzero exit on
failure."""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["LOOM_HOME"] = tempfile.mkdtemp(prefix="loomtest-libclose-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loom import library  # noqa: E402
from loom import chat as chatmod  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL") + f"  {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


from loom.app import Bus, JsApi  # noqa: E402

api = JsApi(Bus())
_orig_release = library.release_lock

with tempfile.TemporaryDirectory(prefix="loomtest-libcloselib-") as d:
    r = api.library_create(d + "/lib")
    check("library created", r["ok"], str(r))

    # a fake mid-generation worker: it exits shortly after its cancel
    # event fires, like a real one whose SSE stream just closed - the
    # sleep stands in for the final _save_from_loop write
    order = []
    cancel = threading.Event()

    def worker():
        cancel.wait(10)
        time.sleep(0.2)
        order.append("worker-final-save")

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    with chatmod._lock:
        chatmod._running["fake-chat"] = {"cancel": cancel, "thread": th}

    def recording_release(root):
        order.append("lock-released")
        _orig_release(root)
    library.release_lock = recording_release

    t0 = time.monotonic()
    r = api.library_close()
    took = time.monotonic() - t0
    library.release_lock = _orig_release
    with chatmod._lock:
        chatmod._running.pop("fake-chat", None)

    check("library_close succeeds", r["ok"], str(r))
    check("stop() fired the worker's cancel", cancel.is_set())
    check("the worker's final save lands BEFORE the lock releases",
          order == ["worker-final-save", "lock-released"], str(order))
    check("the join is prompt, not a timeout ride",
          took < 3.0, f"{took:.2f}s")

    # ---------- a WEDGED worker: the bounded budget must win ----------
    r = api.library_open(d + "/lib")
    check("library reopens", r["ok"], str(r))
    stuck = threading.Event()   # never set - this worker ignores cancel

    def wedged():
        stuck.wait(30)

    th2 = threading.Thread(target=wedged, daemon=True)
    th2.start()
    with chatmod._lock:
        chatmod._running["wedged-chat"] = {"cancel": threading.Event(),
                                           "thread": th2}
    t0 = time.monotonic()
    r = api.library_close()
    took = time.monotonic() - t0
    stuck.set()
    with chatmod._lock:
        chatmod._running.pop("wedged-chat", None)
    check("a wedged worker cannot hold the switch hostage",
          r["ok"] and took < 7.0, f"{took:.2f}s {r}")

if FAILS:
    print("\nFAILURES:", ", ".join(FAILS))
    sys.exit(1)
print("\nALL PASS")
