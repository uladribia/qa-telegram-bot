# knowledge-bot

A channel-agnostic **knowledge bot**. It answers questions from a curated
knowledge base, **always with a source**, and abstains instead of inventing.
Anyone can flag a wrong answer; only an admin can approve a correction.

Telegram is the only runtime adapter in v1. The core is channel-agnostic:
importers and future channels feed the same domain.

Runs entirely inside the **Cloudflare free tier** (Python Worker + D1 + Vectorize
+ Workers AI). No paid fallback exists anywhere in the code.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/setup.md](docs/setup.md) | Deploy and set up from a clean checkout |
| [docs/usage.md](docs/usage.md) | What the bot does, what you type, the typical flows |
| [docs/knowledge-base.md](docs/knowledge-base.md) | Adding and correcting knowledge |
| [docs/operations.md](docs/operations.md) | Quota, logs, troubleshooting, routine maintenance |
| [docs/development.md](docs/development.md) | Layout, rules, gates, how to change the code |
| [AGENTS.md](AGENTS.md) | How to write code here |
| [instructions/](instructions/) | The binding implementation plan and current status |

---

## Quick start

```bash
uv sync
cp .env.example .env          # fill it in; .env is never committed
make all                      # lint + fast tests

uv run pywrangler sync
uv run pywrangler deploy
BOT_BASE_URL=https://<worker>.workers.dev uv run kb set-webhook

BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --qa data/seed/qa.json
make reindex
```

Full instructions, including the one-time Cloudflare resources, are in
[docs/setup.md](docs/setup.md).

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
| `make smoke` | Build the dev image, boot the Worker, check `/healthz` |
| `make reindex` | Rebuild the derived vector index from D1 |
| `make eval-live` | Live quality gate (real model calls; costs AI quota) |
| `make eval-live-reindex` | Same, rebuilding the index first |
| `uv run pywrangler dev --local --ip 0.0.0.0 --port 8787` | Run the Worker locally |

Routes: `GET /healthz`; key-guarded `POST /internal/{recap,reindex,seed,retrieve,eval/answer,eval/judge}`.

---

## Two things to know before you start

**1. The AI budget is real and shared.** The free plan allows 10,000 neurons per
day. When it runs out the bot can answer nothing until 00:00 UTC. Evals and
reindex are refused early by a guard; user questions are never refused. Run
`make eval-live` at most once or twice a day. Details in
[docs/operations.md](docs/operations.md).

**2. D1 is the source of truth.** Vectorize is a derived index, rebuildable at any
time from D1. Never store anything that exists only in the index.

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
