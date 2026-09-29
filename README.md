# knowledge-bot

A channel-agnostic **knowledge bot**. It answers questions from a curated
knowledge base, **always with a source**, and abstains instead of inventing.
Anyone can flag a wrong answer. Local reviewers, global reviewers, and the admin can approve corrections within their scopes.

Telegram is the only runtime adapter in v1. The core is channel-agnostic:
importers and future channels feed the same domain.

How much the bot does in a group is a per-group mode — `off`, `silent`,
`active` (the default: answer when addressed, learn from the rest), or
`proactive` (also answer a confident unaddressed question, and only a grounded
one). Private chats have their own mode. The bot learns who belongs to a group
by seeing them there, because it has no admin rights and does not ask.

Runs locally with SQLite + NumPy + Ollama, or in production inside the
**Cloudflare free tier** (Python Worker + D1 + Vectorize + Workers AI). No paid
fallback exists anywhere in the code.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/setup-local.md](docs/setup-local.md) | Canonical Docker-only local setup |
| [docs/setup-cloudflare.md](docs/setup-cloudflare.md) | Cloudflare deployment |
| [docs/architecture.md](docs/architecture.md) | Runtime and dependency boundaries |
| [docs/usage.md](docs/usage.md) | What the bot does, what you type, the typical flows |
| [docs/knowledge-base.md](docs/knowledge-base.md) | Adding and correcting knowledge |
| [docs/operations.md](docs/operations.md) | Quota, logs, troubleshooting, routine maintenance |
| [docs/development.md](docs/development.md) | Layout, rules, gates, how to change the code |
| [docs/experiments.md](docs/experiments.md) | Measured experiments and the decisions they justify |
| [docs/session-handoff.md](docs/session-handoff.md) | Current hardening state and next-session starting point |
| [docs/e2e-telegram.md](docs/e2e-telegram.md) | Automated real-Telegram E2E (Telethon) |
| [AGENTS.md](AGENTS.md) | How to write code here |
| [instructions/](instructions/) | The binding implementation plan and current status |

---

## Quick start

The canonical local runtime needs Docker but no Cloudflare account:

```bash
cp .env.local.example .env.local
make dev-bootstrap
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/readyz
```

`make dev-bootstrap` starts the pinned local Ollama container, pulls the two
local models, applies SQLite migrations, and serves the API on port 8000.

For production, follow [docs/setup-cloudflare.md](docs/setup-cloudflare.md):

```bash
uv sync
cp .env.example .env          # fill it in; .env is never committed
make all
make deploy                   # ALLOW_CLOUDFLARE_LIVE_TESTS=1
```

---

## Commands

| Command | Purpose |
|---|---|
| `make all` | Lint plus the fast test tier — run after every change |
| `make test` | Fast tier: unit + architecture |
| `make test-integration` | In-process flows with in-memory fakes |
| `make test-all` | Every test tier |
| `make lint` | Format check, lint, type check |
| `make format` | Format and auto-fix |
| `make smoke` | Build the Cloudflare dev image, boot the Worker, check `/healthz` |
| `make dev-bootstrap` | Start local Ollama, build the app, migrate, and serve on port 8000 |
| `make dev-up` / `make dev-down` | Start or stop the local app and Ollama containers |
| `make dev-migrate` | Apply shared and local SQLite migrations |
| `make test-e2e-local` | Explicit local-only Ollama smoke test |
| `make telegram-e2e-login` | One-time login of the human account for the real-Telegram E2E (interactive) |
| `make test-e2e-telegram` | Real black-box E2E against the deployed Worker over real Telegram; needs `ALLOW_CLOUDFLARE_LIVE_TESTS=1`, `.env.e2e`, and the `[E2E]` groups |
| `make reindex` | Clean and batch-rebuild the derived vector index; requires explicit remote authorization |
| `make seed-self-qa` | Seed the bot's self-explanation Q&A (global; run after each release) |
| `make eval-frozen-local` | Same frozen suite against the local stack (no authorization, no quota) |
| `make eval-live-frozen` | Generator-only live gate on the deployed Worker: frozen evidence, no retrieval (one model call per case) |
| `make eval-live` | Live quality gate on the human gold set (real model calls; costs AI quota) |
| `make eval-live-reindex` | Same, after the explicitly authorized bounded reindex |
| `uv run pywrangler dev --local --ip 0.0.0.0 --port 8787` | Run the Worker locally |

Routes: `GET /healthz`; generic key-guarded `POST /v1/{questions,feedback,...}`; operational `POST /internal/jobs/daily-report`; and the Telegram webhook at `POST /adapters/telegram/webhook`. Runtime generation exposes no LLM-judge endpoint.

---

## Two things to know before you start

**1. The AI budget is real and shared.** The free plan allows 10,000 neurons per
day. When it runs out the bot can answer nothing until the next reset. Live
evals and reindex require explicit authorization and `BOT_BASE_URL`; user
questions are never refused. Run them **only with explicit authorization, for
substantive changes that can affect answer quality** — never scheduled, never
auto-retried. Details in [docs/operations.md](docs/operations.md).

**2. SQL is the source of truth for the active runtime.** Production uses D1;
local development uses SQLite. The vector index is derived and rebuildable.
Never store anything that exists only in the index.

**3. Traces are on, and while testing they carry the conversation.**
`logfire` exports request spans and, with `KB_LOGFIRE_CAPTURE_CONTENT`, the
message text, sender identity, prompts, and answers of every flow — that is how
a debugging trace is meant to be read. Tokens, the webhook secret, and API keys
are always scrubbed. It is a testing posture, not a product decision: turn
content capture off before connecting anything but a private test group. Details
in [docs/operations.md](docs/operations.md#traces-logfire).

---

## Current deployment

This repository is deployed as a test Worker:

- Worker name: `bhc-qa-testbot`
- URL: https://bhc-qa-testbot.qa-bots.workers.dev

The Python package stays generic (`knowledge_bot`); only the deploy name is
specific to this test. Secrets are set with `npx wrangler secret put <NAME>` and
never committed.

---

## Built with coding agents

This repository is mostly written by coding agents. The work was driven with the
**`pi`** coding-agent harness, with thanks to its creators, and powered by the
**GLM** and **DeepSeek** models. The implementation plan was written by a human and
agent output is reviewed before it lands, but treat this code as a prototype built
with AI assistance rather than hand-crafted, battle-hardened software.
