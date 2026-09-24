# SPDX-License-Identifier: MIT

.DEFAULT_GOAL := all

.PHONY: all format lint test test-integration test-all test-e2e-local eval-live eval-live-reindex reindex smoke smoke-cloudflare seed-self-qa dev-bootstrap dev-up dev-down dev-logs dev-shell dev-reset dev-migrate dev-seed

all: lint test

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check

# Fast tier: no external services. Use after every change.
test:
	uv run pytest -m "unit or architecture"

# In-process flows with in-memory fakes. Use when touching flows.
test-integration:
	uv run pytest -m integration

# Everything, including slow tiers. Use at milestone boundaries.
test-all:
	uv run pytest

# Live quality gate: real model calls, burns Workers AI quota (10k neurons/day on
# the free plan). Run at most once or twice a day. Reindexing is opt-in because
# re-embedding every record is the largest single draw: `make eval-live-reindex`.
# Requires BOT_BASE_URL (defaults to the deployed Worker).
eval-live:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run python -m evals.run live --base-url "$(BOT_BASE_URL)"

eval-live-reindex:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run python -m evals.run live --reindex --base-url "$(BOT_BASE_URL)"

# Rebuild the derived vector index from D1 (D1 stays the source of truth).
reindex:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run kb reindex --base-url "$(BOT_BASE_URL)"

# Seed the bot's self-explanation Q&A (data/seed/bot_self_qa.json) into the
# deployed Worker as global knowledge. Idempotent: run it after every release
# tag and after any change to the self-explanation entries.
seed-self-qa:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run kb seed --qa data/seed/bot_self_qa.json --base-url "$(BOT_BASE_URL)"

# Runtime fidelity: build the dev image, run the Worker, check /healthz.
smoke:
	bash scripts/smoke.sh

smoke-cloudflare:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	BOT_BASE_URL="$(BOT_BASE_URL)" uv run kb smoke-cloudflare --base-url "$(BOT_BASE_URL)"

# Local SQLite + Ollama runtime.
dev-bootstrap:
	bash scripts/local-dev.sh bootstrap

dev-up:
	bash scripts/local-dev.sh up

dev-down:
	bash scripts/local-dev.sh down

dev-logs:
	bash scripts/local-dev.sh logs

dev-shell:
	bash scripts/local-dev.sh shell

dev-reset:
	bash scripts/local-dev.sh reset

dev-migrate:
	bash scripts/local-dev.sh migrate

dev-seed:
	bash scripts/local-dev.sh seed

# Explicit local-only AI check; ordinary tests never call Ollama.
test-e2e-local:
	bash scripts/local-dev.sh e2e-local
