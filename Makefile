# SPDX-License-Identifier: MIT

.DEFAULT_GOAL := all

.PHONY: all format lint test test-integration test-all eval-live eval-live-reindex reindex smoke

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
	uv run python -m evals.run live --base-url $${BOT_BASE_URL:-https://bhc-qa-testbot.qa-bots.workers.dev}

eval-live-reindex:
	uv run python -m evals.run live --reindex --base-url $${BOT_BASE_URL:-https://bhc-qa-testbot.qa-bots.workers.dev}

# Rebuild the derived vector index from D1 (D1 stays the source of truth).
reindex:
	uv run kb reindex

# Runtime fidelity: build the dev image, run the Worker, check /healthz.
smoke:
	bash scripts/smoke.sh
