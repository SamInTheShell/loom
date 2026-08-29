# Containers & build orchestration

Language-agnostic container practice: Makefile conventions that make a
repo self-explanatory, podman/docker handling that works on any machine,
local Kubernetes with kind, and serving a directory with nginx.

- **makefile-conventions.md** — the house Makefile style: `make` alone
  prints help (always), self-documenting `##` targets, auto-detecting
  podman vs docker and how users override it, `?=` variables, and the
  `localhost/` image-tag rule that keeps podman and docker equivalent.
- **kind-clusters.md** — `kind-up` / `kind-down` for a local Kubernetes
  cluster: idempotent creation, per-run image tags so every run rolls
  the cluster onto the code in your tree, the image side-load that
  works under BOTH docker and podman, and the host-port mapping trap.
- **nginx-serve.md** — the one-liner for serving the project directory
  over http://localhost:8080: `--rm -it`, read-only mount, loopback
  only.
