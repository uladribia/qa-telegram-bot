# SPDX-License-Identifier: MIT

.DEFAULT_GOAL := all

.PHONY: all format lint test test-integration test-all test-e2e-local telegram-e2e-login test-e2e-telegram eval-local eval-live eval-live-frozen eval-frozen-local eval-live-gold eval-gold-local eval-live-reindex reindex smoke smoke-cloudflare deploy set-webhook seed-self-qa dev-bootstrap dev-up dev-down dev-logs dev-shell dev-reset dev-migrate dev-seed decision-smoke decision-eval eval-clef-production-dry eval-clef-production

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

# Local-only quality evals (Ollama + SQLite; zero Cloudflare usage). Regenerates
# reports/retrieval-classifier-listener.md. Requires the local Ollama runtime.
eval-local:
	uv run python -m evals.quality

# Focused Clef-Flash decision evaluation against the deployed Worker. It only
# measures: the listener stays on the baseline, the endpoint persists nothing,
# and the run is bounded by a dry-run cost check. Never added to CI or make all.
eval-clef-production-dry:
	@test -n "$(BOT_BASE_URL)" || \
		(echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run python -m evals.decision_production \
		--dry-run \
		--base-url "$(BOT_BASE_URL)"

eval-clef-production:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || \
		(echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || \
		(echo "BOT_BASE_URL is required" >&2; exit 2)
	@test -n "$(INTERNAL_ADMIN_KEY)" || \
		(echo "INTERNAL_ADMIN_KEY is required" >&2; exit 2)
	uv run python -m evals.decision_production \
		--base-url "$(BOT_BASE_URL)"

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

# The human gold set against the deployed Worker: 11 answerable cases and 15
# abstentions, retrieval included. Roughly 800-1000 neurons, the same cost as
# the frozen suite but measuring this knowledge base rather than the generator.
eval-live-gold:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run python -m evals.run live --suite gold --base-url "$(BOT_BASE_URL)"

# Generator-only gate: every case carries its own evidence, so retrieval does not
# participate and a failure is the model's. One generation call per case, so it
# is the cheapest live suite: run it before touching anything else.
eval-live-frozen:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run python -m evals.run live --suite frozen --base-url "$(BOT_BASE_URL)"

# The same suite against the local stack. No authorization: a local base URL is
# not a remote call, and the local run costs no Workers AI neurons. Same dataset
# and same runner as eval-live-frozen, so the two results are comparable.
eval-frozen-local:
	docker exec knowledge-bot-local .venv/bin/python -m evals.run live \
		--suite frozen --base-url http://127.0.0.1:8000

# The human gold set against the local stack: 11 answerable cases and 15
# abstentions, retrieval included, so it is the only suite that measures this
# knowledge base rather than the generator.
eval-gold-local:
	docker exec knowledge-bot-local .venv/bin/python -m evals.run live \
		--suite gold --base-url http://127.0.0.1:8000

# Rebuild the derived vector index from D1 (D1 stays the source of truth).
reindex:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run kb reindex --base-url "$(BOT_BASE_URL)"

# Ship the Worker to Cloudflare. This is the only supported deploy command:
# `pywrangler` vendors the Python dependencies into src/vendor before building,
# and a bare `npx wrangler deploy` ships a Worker with no vendored code. Needs
# CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the environment.
#
# Run the gates before this, not after: `make all` and `make smoke`.
#
# After a deploy that moves or adds a route, confirm it is live BEFORE
# repointing anything at it. `make set-webhook` does not wait for you.
deploy:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	uv run pywrangler deploy

# Point Telegram at the deployed webhook. Telegram keeps whatever URL it was
# last given, so a deploy that moves the webhook path leaves the old URL
# registered and the new route unused. Order matters: deploy first, then this.
# A wrong order points Telegram at a 404 and the bot fails silently.
set-webhook:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run kb set-webhook

# Seed the bot's self-explanation Q&A (data/seed/bot_self_qa.json) into the
# deployed Worker as global knowledge. Idempotent: run it after every release
# tag and after any change to the self-explanation entries. --renew updates
# entries whose answer text changed.
seed-self-qa:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run kb seed --qa data/seed/bot_self_qa.json --renew --base-url "$(BOT_BASE_URL)"

# Runtime fidelity: build the dev image, run the Worker, check /healthz.
smoke:
	bash scripts/smoke.sh

smoke-cloudflare:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || (echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || (echo "BOT_BASE_URL is required" >&2; exit 2)
	BOT_BASE_URL="$(BOT_BASE_URL)" uv run kb smoke-cloudflare --base-url "$(BOT_BASE_URL)"

# Local System-One decision service. The smoke is the runtime gate (does the
# service answer one canonical request?); the eval scores the local model on
# the intent test split for information only, never as a release gate.
decision-smoke:
	uv run python scripts/eval_decision_service.py

decision-eval:
	uv run python scripts/eval_decision_service.py --eval

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

# One-time interactive login of the existing human admin account for the
# real-Telegram E2E harness. The session lives under .e2e/ (gitignored).
telegram-e2e-login:
	uv run --group e2e python -m e2e.telegram.bootstrap

# Real black-box E2E against the deployed Worker over real Telegram.
# Never runs from CI or other make targets; needs explicit live authorization.
test-e2e-telegram:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || \
		(echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	uv run --group e2e ty check e2e/*.py e2e/telegram/*.py
	uv run --group e2e python -m e2e.telegram.run
