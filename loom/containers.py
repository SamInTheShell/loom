"""Container-backed shell execution for chats - never on the host.

Distilled from CodeTree's containers.py to the essentials:

  * podman preferred, docker fallback (loom.yaml `containers.engine` pins).
  * images build from the library's container files (loom.yaml
    `containers.definitions: [{name, file}]`) into `loom/<name>:latest`,
    with an EMPTY build context - a build can never slurp the library in.
  * every command runs `<engine> run --rm` as an UNPRIVILEGED user:
    the image's USER (the scaffolded Containerfile creates uid-1000
    `loom`), reinforced with --user 1000:1000, and `--userns keep-id` on
    rootless podman so mounted files keep sane ownership.
  * networking is OFF by default (see NET_MODES: none / loopback / on -
    per chat or terminal, user-chosen). There is deliberately no yaml key
    to switch it on - library content must not widen a security boundary.
  * attached folders mount at /mnt/<name> (ro for view mode, rw for write
    mode); the library knowledge base mounts read-only at /knowledge and
    the chat's artifact folder read-write at /artifacts; a per-chat home
    persists at ~/.loom/homes/<chat-id> so state survives between
    commands within a chat.
  * timeout / cancel: `<engine> rm -f <name>` - the unique per-exec name
    means we always kill exactly ours.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from loom import store

EXEC_TIMEOUT_DEFAULT = 300

# ---------------------------------------------------------------------------
# network modes. Three, not two:
#   none      --network=none. The container keeps its OWN private loopback
#             (an in-container server + curl 127.0.0.1 works); nothing
#             outside the container is reachable.
#   loopback  the HOST's loopback services (a llama-server on
#             127.0.0.1:8080, a local database...) are reachable at
#             10.0.2.2 - and nothing else is. Implemented with
#             slirp4netns: allow_host_loopback maps the host loopback in,
#             and outbound_addr=127.0.0.1 binds every outbound socket to
#             the host's loopback, which makes external destinations
#             unroutable AT THE KERNEL - no firewall rules, fully
#             rootless. podman only (docker has no rootless equivalent).
#   on        the engine's default network.

NET_MODES = ("none", "loopback", "on")

LOOPBACK_NET_ARG = ("--network=slirp4netns:allow_host_loopback=true,"
                    "outbound_addr=127.0.0.1")


def net_mode(value) -> str:
    """Normalize a chat/terminal network setting. Legacy booleans (the
    old on/off toggle) map to on/none; unknown values fail closed."""
    if value is True:
        return "on"
    if isinstance(value, str) and value in NET_MODES:
        return value
    return "none"


def net_args(engine: str, value) -> list[str]:
    """The engine argv for a network mode."""
    mode = net_mode(value)
    if mode == "on":
        return []
    if mode == "none":
        return ["--network=none"]
    if "docker" in str(engine or ""):
        raise ContainerError(
            "loopback-only networking needs podman (slirp4netns) - docker "
            "has no rootless equivalent. Switch this chat to no network "
            "or network on, or use podman.")
    return [LOOPBACK_NET_ARG]
EXEC_TIMEOUT_MAX = 3600
MAX_OUTPUT = 200_000
BUILD_TIMEOUT = 1800


class ContainerError(Exception):
    pass


_detect_lock = threading.Lock()
_detected: dict[str, str | None] = {}


def resolve_engine(preference: str = "auto") -> str:
    """The container runtime binary to use, or raise with advice."""
    pref = (preference or "auto").lower()
    with _detect_lock:
        if pref in _detected:
            found = _detected[pref]
        else:
            order = ["podman", "docker"] if pref == "auto" else [pref]
            found = next((b for b in order if shutil.which(b)), None)
            _detected[pref] = found
    if not found:
        raise ContainerError(
            "no container engine found - install podman (preferred) or "
            "docker, or set containers.engine in loom.yaml")
    return found


def _is_rootless(engine: str) -> bool:
    if "podman" not in engine:
        return False
    try:
        out = subprocess.run([engine, "info", "--format",
                              "{{.Host.Security.Rootless}}"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip().lower() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return True   # assume the safer, more common setup


def _user_args(engine: str) -> list[str]:
    if _is_rootless(engine):
        return ["--userns", "keep-id:uid=1000,gid=1000", "--user", "1000:1000"]
    return ["--user", "1000:1000"]


_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def image_tag(name: str) -> str:
    if not _NAME_RE.match(str(name or "")):
        raise ContainerError(f"bad container definition name: {name!r}")
    return f"loom/{name}:latest"


def image_exists(engine: str, tag: str) -> bool:
    try:
        return subprocess.run([engine, "image", "exists", tag],
                              capture_output=True, timeout=20).returncode == 0 \
            if "podman" in engine else \
            bool(subprocess.run([engine, "image", "inspect", tag],
                                capture_output=True, timeout=20).returncode == 0)
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_image(root: Path, definition: dict, engine_pref: str = "auto",
                notice=None) -> str:
    """Build a library container definition. Returns the image tag."""
    from loom import library as _lib
    engine = resolve_engine(engine_pref)
    name = str(definition.get("name") or "")
    tag = image_tag(name)
    file_rel = str(definition.get("file") or "")
    if not file_rel:
        raise ContainerError(f"container {name!r} has no `file` in loom.yaml")
    cfile = _lib.safe_join(root, file_rel)
    if not cfile.is_file():
        raise ContainerError(f"container file not found: {file_rel}")
    if notice:
        notice(f"building {tag} from {file_rel}…")
    # empty build context: the -f file is all the build may see
    ctx = store.run_dir() / f"buildctx-{uuid.uuid4().hex[:8]}"
    ctx.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [engine, "build", "-t", tag, "-f", str(cfile), str(ctx)],
            capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ContainerError(f"building {tag} timed out")
    finally:
        shutil.rmtree(ctx, ignore_errors=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise ContainerError(f"building {tag} failed:\n{tail}")
    return tag


def ensure_image_named(root: Path, cfg: dict, name: str,
                       notice=None) -> tuple[str, str]:
    """(engine, image tag) for a NAMED container definition, building on
    first use."""
    ct = cfg.get("containers") or {}
    engine = resolve_engine(ct.get("engine") or "auto")
    want = str(name or ct.get("default") or "sandbox")
    definition = next((d for d in ct.get("definitions") or []
                       if d.get("name") == want), None)
    if definition is None:
        raise ContainerError(
            f"no container named {want!r} under containers.definitions "
            "in loom.yaml")
    tag = image_tag(want)
    if not image_exists(engine, tag):
        build_image(root, definition, ct.get("engine") or "auto", notice)
    return engine, tag


def ensure_image(root: Path, cfg: dict, notice=None) -> tuple[str, str]:
    """(engine, image tag) for this library's default sandbox."""
    return ensure_image_named(root, cfg, "", notice)


def chat_home(chat_id: str) -> Path:
    d = store.home() / "homes" / re.sub(r"[^a-zA-Z0-9_-]", "_", chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mount_name(path: str) -> str:
    leaf = Path(path).name or "folder"
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", leaf)


def mounts_for(folders: list[dict]) -> tuple[list[str], list[str]]:
    """(-v args, notes) for a chat's attached folders.
    folders: [{path, mode: 'view'|'write'}]. Names collide → suffixed."""
    args, notes, used = [], [], set()
    for f in folders or []:
        p = Path(str(f.get("path") or "")).expanduser()
        if not p.is_dir():
            continue
        name = _mount_name(str(p))
        n, i = name, 2
        while n in used:
            n, i = f"{name}-{i}", i + 1
        used.add(n)
        rw = str(f.get("mode") or "view") == "write"
        args += ["-v", f"{p.resolve()}:/mnt/{n}:{'rw' if rw else 'ro'}"]
        notes.append(f"/mnt/{n} ({'read-write' if rw else 'read-only'}) = {p}")
    return args, notes


class ExecResult:
    def __init__(self, rc: int, output: str, timed_out: bool, cancelled: bool):
        self.rc = rc
        self.output = output
        self.timed_out = timed_out
        self.cancelled = cancelled


def run_shell(engine: str, image: str, chat_id: str, command: str,
              folders: list[dict] | None = None,
              timeout: int = EXEC_TIMEOUT_DEFAULT,
              cancel: threading.Event | None = None,
              on_output=None, network="none",
              knowledge: Path | None = None,
              artifacts: Path | None = None,
              extra_env: dict | None = None) -> ExecResult:
    """Run one shell command in a fresh container. Streams combined
    stdout+stderr through on_output (capped); TERM on cancel/timeout via
    `<engine> rm -f` so nothing lingers.

    knowledge mounts READ-ONLY at /knowledge; artifacts mounts READ-WRITE
    at /artifacts (the chat's delivery folder). extra_env vars reach the
    container via a 0600 --env-file, NEVER the argv - secrets must not
    show in the host process list."""
    timeout = max(1, min(int(timeout or EXEC_TIMEOUT_DEFAULT), EXEC_TIMEOUT_MAX))
    name = f"loom-exec-{uuid.uuid4().hex[:12]}"
    vol, _notes = mounts_for(folders or [])
    if knowledge is not None and knowledge.is_dir():
        vol += ["-v", f"{knowledge.resolve()}:/knowledge:ro"]
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=True)
        vol += ["-v", f"{artifacts.resolve()}:/artifacts:rw"]
    env_file = None
    if extra_env:
        env_file = store.run_dir() / f"env-{uuid.uuid4().hex[:12]}"
        env_file.touch(mode=0o600)
        env_file.write_text("".join(f"{k}={v}\n"
                                    for k, v in extra_env.items()),
                            encoding="utf-8")
        vol += ["--env-file", str(env_file)]
    home = chat_home(chat_id)
    argv = [engine, "run", "--rm", "--name", name,
            *net_args(engine, network),
            "-v", f"{home}:/home/loom:rw",
            *vol, *_user_args(engine),
            "-e", "HOME=/home/loom", "-w", "/home/loom",
            "--entrypoint", "/bin/bash", image, "-lc", str(command)]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
    except OSError as e:
        if env_file is not None:
            env_file.unlink(missing_ok=True)
        raise ContainerError(f"cannot start {engine}: {e}")

    chunks: list[str] = []
    total = 0
    deadline = time.monotonic() + timeout
    timed_out = cancelled = False

    def _kill():
        subprocess.run([engine, "rm", "-f", name], capture_output=True,
                       timeout=30)

    def _reader():
        nonlocal total
        for line in proc.stdout:
            if total < MAX_OUTPUT:
                keep = line[:MAX_OUTPUT - total]
                chunks.append(keep)
                total += len(keep)
                if on_output:
                    try:
                        on_output(keep)
                    except Exception:
                        pass
    t = threading.Thread(target=_reader, daemon=True, name=f"exec-{name}")
    t.start()
    while True:
        if proc.poll() is not None:
            break
        if cancel is not None and cancel.is_set():
            # EAGER cancel: the kill runs in the background and the caller
            # unblocks immediately - the output is being discarded anyway
            cancelled = True
            threading.Thread(target=_kill, daemon=True,
                             name=f"kill-{name}").start()
            break
        if time.monotonic() > deadline:
            timed_out = True
            threading.Thread(target=_kill, daemon=True,
                             name=f"kill-{name}").start()
            break
        time.sleep(0.05)
    try:
        proc.wait(1.5 if (cancelled or timed_out) else 20)
    except subprocess.TimeoutExpired:
        proc.kill()
    t.join(1 if (cancelled or timed_out) else 5)
    if env_file is not None:
        env_file.unlink(missing_ok=True)   # secrets file lives run-length only
    out = "".join(chunks)
    if total >= MAX_OUTPUT:
        out += "\n[output truncated]"
    return ExecResult(proc.returncode if proc.returncode is not None else -1,
                      out, timed_out, cancelled)
