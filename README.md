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
| `docker build -t knowledge-bot:dev .` | Build the dev image |

Routes: `GET /healthz`, `GET /smoke/deps` (temporary).

## Built with coding agents

This repository is mostly written by coding agents. The work was driven with the
**`pi`** coding-agent harness, with thanks to its creators, and powered by the
**GLM** and **DeepSeek** models. The implementation plan was written by a human and
agent output is reviewed before it lands, but treat this code as a prototype built
with AI assistance rather than hand-crafted, battle-hardened software.
