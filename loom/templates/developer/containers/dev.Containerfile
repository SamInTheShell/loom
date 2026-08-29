# Developer container — Go + Python toolchains for shell commands and
# terminals. Runs as the unprivileged `loom` user, never root.
#
# Containers run with NETWORKING OFF by default (toggle per chat/terminal
# in the UI — never from a config file). Toolchains are installed at BUILD
# time (the build has network); anything you need beyond them either goes
# into this file, or you enable network for the session that fetches it —
# per-session caches persist in the mounted home between commands.
FROM docker.io/library/golang:1-trixie

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 jq ripgrep unzip zip sqlite3 make \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash loom \
    # Loom execs commands through a LOGIN shell (bash -l), and Debian's
    # /etc/profile resets PATH — dropping this image's /usr/local/go/bin
    # entry, which strands `go` off-PATH. Persist it for login shells:
    && printf 'export PATH="/usr/local/go/bin:$PATH"\n' \
        > /etc/profile.d/go-path.sh

USER loom
WORKDIR /home/loom
