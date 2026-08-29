# kind-up / kind-down — local Kubernetes that always runs your tree

Two Makefile targets give a repo a real multi-node Kubernetes cluster on
one machine: `make kind-up` builds images from the working tree, loads
them into a kind cluster (creating it if needed), deploys, and blocks
until every pod runs that build; `make kind-down` deletes it all. The
contract worth defending: **when kind-up returns, the cluster serves the
code in your tree** — re-running it against a live cluster converges, no
kubectl surgery.

## The skeleton

```make
KIND_CLUSTER ?= myapp
IMAGE ?= localhost/myapp:dev
# per-RUN tag: deploying a fresh tag changes the pod templates, so
# Kubernetes itself rolls every workload onto the image just built
KIND_TAG := dev-$(shell date -u +%Y%m%d%H%M%S)

kind-up: docker ## build, create/refresh the cluster, deploy, wait
	# idempotent create: a repeated/failed kind-up converges, not errors
	kind get clusters 2>/dev/null | grep -qx '$(KIND_CLUSTER)' || \
		kind create cluster --config kind.yaml --name $(KIND_CLUSTER)
	# GC image generations from older runs inside the nodes (crictl
	# skips anything a pod still uses) so per-run tags don't pile up
	for node in $$(kind get nodes --name $(KIND_CLUSTER)); do \
		$(DOCKER_CMD) exec $$node crictl rmi --prune >/dev/null 2>&1 || true; \
	done
	# side-load the image (see below for why THIS form)
	$(DOCKER_CMD) tag $(IMAGE) localhost/myapp:$(KIND_TAG)
	$(DOCKER_CMD) save localhost/myapp:$(KIND_TAG) -o /tmp/myapp-kind.tar
	$(DOCKER_CMD) rmi localhost/myapp:$(KIND_TAG)
	kind load image-archive /tmp/myapp-kind.tar --name $(KIND_CLUSTER)
	rm -f /tmp/myapp-kind.tar
	helm upgrade --install myapp charts/myapp \
		--set image.repository=localhost/myapp --set image.tag=$(KIND_TAG)
	# block until the rollout finishes — "returned" must mean "running"
	kubectl rollout status deployment/myapp --timeout=5m

kind-down: ## delete the local kind cluster and everything in it
	kind delete cluster --name $(KIND_CLUSTER)
```

## Why the side-load goes through an archive

kind's nodes pull from their own containerd, never the host engine, so
freshly built images must be loaded into every node. `kind load
docker-image` looks images up in the DOCKER daemon specifically — it
misses podman-built images. `save` to an archive + `kind load
image-archive` is the form that works reliably under **both** engines
(pair it with the `localhost/` tag rule from makefile-conventions.md).

## Why a per-run tag

Kubernetes only restarts pods when the pod template changes. Reloading
the same `:dev` tag changes nothing — pods keep the old image. Retag
each run uniquely (`dev-<timestamp>`), deploy that tag, and the rollout
is automatic; the crictl prune keeps old generations from accumulating.

## The host-port trap

kind's `extraPortMappings` (host → NodePort) apply at **cluster
creation only**. If the cluster predates a change to kind.yaml, those
localhost ports are dead no matter what you deploy — `make kind-down &&
make kind-up` once. For everything else, `kubectl port-forward
svc/myapp 8080:80` covers ad-hoc access; note that sustained transfers
(streaming, large blobs) can wedge port-forward's single SPDY stream —
a raw TCP relay to the NodePort is the streaming-safe alternative.

End every kind-up with an `@echo` block listing the URLs and ports just
deployed — the cluster explaining itself beats a docs lookup.
