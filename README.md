# knowledge-bot

Channel-agnostic knowledge bot backend. Telegram is the only runtime adapter in v1;
importers and future channels feed the same core.

- How to write code here: [`AGENTS.md`](AGENTS.md)
- Binding implementation plan:
  [`instructions/pla_prototip_bot_telegram_bhc_v3.md`](instructions/pla_prototip_bot_telegram_bhc_v3.md)

## Commands

| Command | Purpose |
|---|---|
| `uv sync` | Install dependencies |
| `uv run pywrangler sync` | Vendor dependencies for the Worker runtime |
| `uv run pywrangler dev --local --ip 0.0.0.0 --port 8787` | Run the Worker locally |
| `make format` | Format and auto-fix |
| `make lint` | Format check, lint, type check |
| `make test` | Fast tier: unit + architecture tests |
| `make test-integration` | In-process flows with in-memory fakes |
| `make test-all` | Every test tier |
| `make smoke` | Build the dev image and check the running Worker |
| `make all` | Lint plus the fast test tier |
| `make reindex` | Rebuild the derived vector index from D1 |
| `make eval-live` | Live quality gate (real model calls; costs AI quota) |
| `make eval-live-reindex` | Same, rebuilding the index first |
| `docker build -t knowledge-bot:dev .` | Build the dev image |

Routes: `GET /healthz`, `POST /internal/{recap,reindex,seed,retrieve,eval/answer,eval/judge}`
(key-guarded).

## The AI quota guard

The free plan allows **10,000 neurons per day**. When it runs out, every model
call fails and the bot can answer nothing until 00:00 UTC.

The account's real usage is not visible to a Worker, so calls are metered with a
character-based estimate (`ai_budget` table, `AI_*` settings). A deliberate
reserve is held back for real traffic:

- **evals and reindex are refused with HTTP 429** once `budget - reserve` is
  reached, so a stray eval run cannot take the bot down for a day.
- **a user question is never refused.** It degrades to the
temporary-unavailable reply, so the inbound event is never lost.

Reproduce a spent day for testing:

```bash
npx wrangler d1 execute knowledge-bot --remote --command \
  "INSERT INTO ai_budget (day, neurons, calls, updated_at)\
   VALUES ('2026-01-01', 20000, 1, '2026-01-01T00:00:00Z')\
   ON CONFLICT(day) DO UPDATE SET neurons=excluded.neurons"
```

## Deployment

This repository is currently deployed as a test Worker:

- Worker name: `bhc-qa-testbot`
- URL: https://bhc-qa-testbot.qa-bots.workers.dev
- Deploy: `uv run pywrangler deploy`

The Python package and its code stay generic (`knowledge_bot`); only the deploy
name is specific to this test. Secrets are set with `npx wrangler secret put <NAME>`
and never committed.

One-time infrastructure: the Vectorize index needs metadata indexes before
filtered retrieval returns anything:

```bash
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name kind --type string
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name status --type string
```

## Built with coding agents

This repository is mostly written by coding agents. The work was driven with the
**`pi`** coding-agent harness, with thanks to its creators, and powered by the
**GLM** and **DeepSeek** models. The implementation plan was written by a human and
agent output is reviewed before it lands, but treat this code as a prototype built
with AI assistance rather than hand-crafted, battle-hardened software.
