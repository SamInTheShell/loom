# Serving a directory with nginx

The quick way to eyeball a static site, coverage report, or docs build:
serve the project directory with a throwaway nginx container.

```sh
$(DOCKER_CMD) run --rm -it \
  -p 127.0.0.1:8080:80 \
  -v "$(PWD)":/usr/share/nginx/html:ro \
  docker.io/library/nginx:alpine
```

Then open http://localhost:8080. Ctrl-C stops and removes it.

Every flag is deliberate:

- `--rm -it` - a throwaway, interactive foreground container: logs in
  your terminal, Ctrl-C tears it down, nothing left behind.
- `-p 127.0.0.1:8080:80` - **loopback only**. A bare `-p 8080:80` binds
  every interface and serves your working tree to the whole LAN; the
  `127.0.0.1:` prefix is the difference between a dev preview and an
  accidental file server.
- `:ro` on the mount - nginx (and anything that compromises it) cannot
  write into your tree. Serving never needs write access.
- the fully-qualified image name pulls identically under podman and
  docker (podman has no implicit Docker Hub default).

As a Makefile target, using the engine auto-detection from
makefile-conventions.md:

```make
serve: ## serve this directory at http://localhost:8080 (Ctrl-C stops)
	@echo 'serving → http://localhost:8080  (Ctrl-C to stop)'
	$(DOCKER_CMD) run --rm -it \
		-p 127.0.0.1:8080:80 \
		-v "$(CURDIR)":/usr/share/nginx/html:ro \
		docker.io/library/nginx:alpine
```

For a subdirectory (a `docs/` build, a `dist/` bundle), mount that path
instead of the repo root - the less you serve, the less you leak.
