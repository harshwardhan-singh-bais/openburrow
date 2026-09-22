.DEFAULT_GOAL := help
SHELL := bash

PY := .venv/Scripts/python.exe
ifeq ($(OS),)
	PY := .venv/bin/python
endif

COMPOSE := docker/compose.dev.yml

.PHONY: help
# `0-9` is not decoration. Without it the character class excludes every target
# whose name contains a digit, so `test-e2e` was defined, ran, and never appeared
# here — a help listing that silently omits targets is worse than one that lists
# them in the wrong order. The comment sits above the target rather than inside
# the recipe because make echoes recipe lines, and an echoed comment would land
# in the middle of this listing.
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------
.PHONY: install
install: ## Create the venv and install all 11 workspace packages
	uv sync

.PHONY: lock
lock: ## Re-resolve and write uv.lock
	uv lock

.PHONY: web-install
web-install: ## Install the frontend's dependencies
	cd apps/web && npm install --no-audit --no-fund

# --- verification ----------------------------------------------------------
.PHONY: smoke
smoke: ## End-to-end check: real HTTP, real negotiation, governance invariants
	uv run python scripts/smoke_test.py

.PHONY: probe
probe: ## Debug a lane's A2A server route by route
	uv run python scripts/probe_lane_server.py

.PHONY: test
test: ## Run the full test suite
	uv run pytest

.PHONY: test-unit
test-unit: ## Fast tests only
	uv run pytest -m unit

.PHONY: test-governance
test-governance: ## The governance invariants — mandatory for authority changes
	uv run pytest -m governance

.PHONY: test-chaos
test-chaos: ## Failure-path tests driven by the scriptable mock harness
	uv run pytest -m chaos

.PHONY: test-e2e
test-e2e: ## Spawns the harness binaries installed on this machine
	# The only place the switch has to be remembered. `pytest` skips these by
	# default — see packages/conftest.py — so that a bare `make test` on a
	# laptop, or in CI, never launches Claude Code by accident. Not part of
	# `check` for the same reason: it needs a harness installed and configured,
	# which a clean checkout does not have.
	OPENBURROW_E2E=1 uv run pytest -m e2e

.PHONY: test-relay
test-relay: ## The relay package only, which is the one with integration tests
	uv run pytest packages/openburrow-relay/tests

.PHONY: cov
cov: ## Test suite with a coverage report
	uv run pytest --cov=packages --cov-report=term-missing --cov-report=html

.PHONY: lint
lint: ## Check style and types without changing anything
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages

.PHONY: fmt
fmt: ## Apply formatting and safe lint fixes
	uv run ruff check . --fix
	uv run ruff format .

.PHONY: imports
imports: ## Import every module, so a NameError or circular import cannot hide
	# The only gate that executes code. `lint` and `mypy` are both static and
	# both missed a module-level NameError that made an entire package
	# unloadable, so this runs alongside them rather than instead of them.
	uv run python scripts/check_imports.py

.PHONY: parity
parity: ## Fail if a TypeScript enum mirror drifts from its Python original
	uv run python scripts/check_enum_parity.py

.PHONY: parity-endpoint
parity-endpoint: ## Compare the web app's daemon endpoint against BurrowPaths (needs node)
	# Separate from `parity` because this one has to *execute* the TypeScript
	# rather than read it. The endpoint is a hash of a path, so it cannot be
	# compared statically the way the enum mirrors can — and check_enum_parity.py
	# deliberately avoids a Node dependency so it still runs in a Python-only job.
	#
	# It exits 2, not 1, when it cannot run at all (no node on PATH). A missing
	# toolchain and a real divergence need different responses, so they are
	# different codes.
	uv run python scripts/check_endpoint_parity.py

.PHONY: falsify
falsify: ## Re-verify that the newest tests fail against the code they replaced
	# Deliberately NOT part of `check`. A falsification pass reconstructs the
	# behaviour a fix removed, so it is only meaningful while that fix is the
	# most recent change to the file; run against code that has moved on, it
	# asserts a boundary that no longer exists. A stale gate that passes is the
	# failure mode this whole project keeps auditing for.
	#
	# One script per fix, run together. They work differently on purpose: stage 12
	# patches methods at runtime, stage 13 rewrites module source and executes it,
	# the stale-lane harness drives the real supervisor against a real database,
	# and the endpoint harness mutates a file and re-runs the check as a
	# subprocess so the mutation is visible to a fresh import.
	#
	# The knowledge-injection harness uses that last mode too, for a reason worth
	# naming: its central row asserts the *call site* in `sessions.py`, so the
	# module under test has to be re-read from disk by a fresh interpreter. A
	# reverted in-process module would not be the module those tests inspect.
	uv run python scripts/falsify_stage12.py
	uv run python scripts/falsify_stage13.py
	uv run python scripts/falsify_stale_lanes.py
	uv run python scripts/falsify_endpoint_parity.py
	uv run python scripts/falsify_knowledge_injection.py

.PHONY: typecheck-web
typecheck-web: ## Typecheck the frontend
	cd apps/web && npx tsc --noEmit

.PHONY: lint-web
lint-web: ## Lint the frontend
	cd apps/web && npm run lint

.PHONY: test-web
test-web: ## Run the frontend test suite
	# Needs `apps/web/node_modules`, so run `make web-install` once first.
	# `npm test` is `vitest run`, not `vitest` — watch mode would hang a gate.
	# The suites need no daemon and no network: they build their own temp
	# trees and import the route handlers directly.
	cd apps/web && npm test

.PHONY: check-web
check-web: typecheck-web lint-web test-web ## Everything the frontend job runs
	@echo ""
	@echo "  frontend checks passed"

.PHONY: check
check: lint imports parity parity-endpoint docs-commands test test-web ## Everything CI runs locally
	@echo ""
	@echo "  all checks passed"

# --- running ---------------------------------------------------------------
.PHONY: cli
cli: ## Show the CLI surface
	uv run burrow --help

.PHONY: doctor
doctor: ## Check this machine's environment
	uv run burrow doctor

.PHONY: daemon
daemon: ## Start the daemon in the foreground
	uv run burrow daemon start --foreground

.PHONY: relay
relay: ## Start the relay locally
	# The relay is its own package with its own entry point, not a `burrow`
	# subcommand — it is the one component that runs somewhere other than the
	# machine with the repo, so it is not part of the CLI's command tree.
	uv run openburrow-relay --host 127.0.0.1 --port 8787

.PHONY: relay-check
relay-check: ## Validate the relay's configuration and exit
	uv run openburrow-relay --check

.PHONY: web
web: ## Start the frontend dev server
	cd apps/web && npm run dev

# --- build -----------------------------------------------------------------
.PHONY: build
build: ## Build all workspace packages
	uv build --all --out-dir dist

.PHONY: build-web
build-web: ## Production build of the frontend
	cd apps/web && npm run build

.PHONY: docs
docs: ## Build the documentation site
	uv run --group docs mkdocs build --strict

.PHONY: docs-serve
docs-serve: ## Serve the documentation with live reload
	uv run --group docs mkdocs serve

.PHONY: docs-links
docs-links: ## Fail if a relative link in the docs does not resolve
	uv run python scripts/check_docs_links.py

.PHONY: docs-commands
docs-commands: ## Fail if a documented `burrow` command does not exist
	# The README's Quick Start told readers to run `burrow session start
	# --template pair` and `burrow watch`. Neither exists: the real names are
	# `burrow session start --lanes ...` and `burrow observability watch`, and
	# `--template` was invented outright. Nothing reads documentation, so
	# nothing noticed that the third line of the Quick Start exited 2. This
	# reads it — every `burrow ...` inside a fenced block, checked against the
	# command tree the CLI actually registers.
	uv run python scripts/check_docs_commands.py

# --- containers ------------------------------------------------------------
.PHONY: docker
docker: ## Build the relay image
	docker build -f docker/Dockerfile --target relay -t openburrow-relay:local .

.PHONY: docker-web
docker-web: ## Build the frontend image
	docker build -f docker/Dockerfile --target web -t openburrow-web:local .

.PHONY: up
up: ## Start relay + Postgres for development
	docker compose -f $(COMPOSE) up -d

.PHONY: down
down: ## Stop the development stack
	docker compose -f $(COMPOSE) down

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: clean-all
clean-all: clean ## Also remove the venv — you will need to reinstall
	rm -rf .venv
