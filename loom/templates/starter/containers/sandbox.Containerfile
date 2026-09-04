# Default sandbox image for shell commands run from chats and terminals.
# Loom builds this and runs every shell inside it, as the unprivileged
# user below - never root, never on the host.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils findutils grep sed gawk curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash loom
USER loom
WORKDIR /home/loom
