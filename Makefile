# Commands the coding agent runs (and CI runs the same ones). Keep them offline and non-interactive.
SHELL := /bin/bash
PY ?= python3
SECRET_RE := ghp_|github_pat_|sk-or-[a-z0-9]|BEGIN .*PRIVATE KEY|@[a-z0-9-]+\.(com|io|pl)
PLACEHOLDERS := you@example\.com|noreply

.PHONY: setup check syntax config secrets secrets-history docs run vm-up vm-down vm-reset vm-deploy vm-status vm-logs docker-test docker-down

setup: ## nothing to install; just make sure the tools exist
	@$(PY) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11+ required")'
	@command -v bash >/dev/null || { echo "bash is required" >&2; exit 1; }
	@echo "ok: $$($(PY) --version 2>&1), bash; nothing to install"

check: syntax config secrets docs ## what the board and CI run before a PR
	@echo "make check: ok"

syntax:
	bash -n install.sh deploy.sh repo-kit/apply.sh scripts/vm.sh tests/scenario.sh tests/deploy-vm.sh tests/docker.sh
	$(PY) -c "import ast; ast.parse(open('bridge.py').read())"

config:
	$(PY) -c "import tomllib; [tomllib.load(open(p, 'rb')) for p in ('config/autonomous.example.toml', 'tests/autonomous.test.toml.in')]"
	$(PY) -c "import json; [json.load(open(p)) for p in ('.herdr-projects.json', 'repo-kit/.herdr-projects.json')]"

secrets: ## working tree + new files; ignored files such as config/env are not searched
	@hits=$$(git grep --untracked -n -I -i -E '$(SECRET_RE)' -- . ':!Makefile' | grep -v -E '$(PLACEHOLDERS)' || true); \
	if [ -n "$$hits" ]; then printf '%s\n' "$$hits"; echo "possible secret above" >&2; exit 1; fi
	@echo "no secrets in the working tree"

secrets-history: ## slower: every commit; run before pushing
	@hits=$$(git grep -n -I -i -E '$(SECRET_RE)' $$(git rev-list --all) -- . ':!Makefile' ':!AGENTS.md' | grep -v -E '$(PLACEHOLDERS)' || true); \
	if [ -n "$$hits" ]; then printf '%s\n' "$$hits"; echo "possible secret in history above" >&2; exit 1; fi
	@echo "no secrets in history"

docs:
	@lines=$$(wc -l < CHEATSHEET.md); if [ "$$lines" -gt 80 ]; then \
	  echo "CHEATSHEET.md is $$lines lines; keep it to one A4 page (~80 lines)" >&2; exit 1; fi; \
	  echo "CHEATSHEET.md: $$lines lines"

run: ## the bridge runs on the VPS, not on the machine you are developing on
	@echo "The bridge runs on the VPS. Deploy with: ./deploy.sh <host>"
	@echo "Never deploy to the host you are working on: it restarts herdr and gh-bridge."

# Throwaway Linux VM for e2e tests (see tests/README.md).
vm-up: ; scripts/vm.sh up
vm-down: ; scripts/vm.sh down
vm-reset: ; scripts/vm.sh reset
vm-deploy: ; scripts/vm.sh deploy
vm-status: ; scripts/vm.sh status
vm-logs: ; scripts/vm.sh logs

# Fast local e2e in a privileged systemd container (no KVM); see tests/README.md.
docker-test: ; tests/docker.sh up
docker-down: ; tests/docker.sh down
