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
| `make format` | Format and auto-fix |
| `make lint` | Format check, lint, type check |
| `make test` | Run tests |
| `make all` | Lint and test (the quality gate) |
