# Loom - development targets.

UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help run debug install test compose

help: ## Show this help
	@echo "Loom"
	@echo
	@awk 'BEGIN { FS = ":.*## " } \
	     /^[a-zA-Z_-]+:.*## / { printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

run: ## Launch the app (attached)
	$(UV) run loom --foreground

debug: ## Launch with DevTools open
	$(UV) run loom --debug

install: ## Install the loom CLI for the current user, replacing any old copy
	$(UV) tool install --force .

compose: ## Regenerate frontend/index.html from the templates
	$(UV) run python -m loom.compose

# The suite is SCRIPT-STYLE on purpose (each file is its own runner with a
# check() helper and a nonzero exit on failure) - pytest collects zero
# tests here and exits green, which is exactly the trap this target closes.
test: ## Run every test
	@fail=0; \
	for t in tests/test_*.py; do \
	  echo "== $$t"; $(UV) run python "$$t" || fail=1; \
	done; \
	if command -v node >/dev/null 2>&1; then \
	  for t in tests/test_*.js; do \
	    echo "== $$t"; node "$$t" || fail=1; \
	  done; \
	else echo "(node not found - skipping js tests)"; fi; \
	if [ $$fail -eq 0 ]; then echo; echo "ALL PASS"; \
	else echo; echo "FAILURES above"; exit 1; fi
