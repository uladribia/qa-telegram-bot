# Session handoff

_Last updated: 2026-09-24 on `fix/final-hardening-pass`._

## Current state

- Base revision: `f5224fe0a9cc184e1f8011140ae24b620a0a16e2` from `main`.
- Binding plan: [`instructions/qa-telegram-bot-one-pass-final-fix-plan.md`](../instructions/qa-telegram-bot-one-pass-final-fix-plan.md).
- Branch: `fix/final-hardening-pass`.
- The deprecated plans are not implementation contracts and are not referenced by `AGENTS.md`.

## Work completed

- Reindex cleanup is separate from bounded batch projection; no empty request performs a destructive rebuild.
- Local Q&A retrieval overrides only the same canonical global question and ranks the final list by similarity and authority.
- Listener windows distinguish successful zero-pair processing from unavailable processing and preserve `context_question` through projection failure.
- Semantic correction approval survives projection failure and reports projection status separately.
- Recognized Telegram callbacks acknowledge once, including unavailable and already-resolved targets.
- Background backlog and reindex requests are capped at 100.
- Added delivery retry coverage, local synthetic Telegram E2E coverage, offline CI, privacy-safe boundary logging, and a local test transport injection point.
- Removed the dead reviewer report service and switched recap delivery to the channel-neutral notifier.

## Verification

Passed locally:

```text
make format
make lint
make test                 # 122 passed
make test-integration     # 120 passed
```

`make dev-bootstrap`, `make test-e2e-local`, and `make smoke` passed locally. No Cloudflare, Telegram network, live eval, remote reindex, or production mutation was performed.

## Remaining acceptance

- Complete the human-operated real Telegram two-group acceptance flow in [`docs/e2e-telegram.md`](e2e-telegram.md).
- Run Cloudflare smoke or live evaluation only after explicit human authorization and an explicit `BOT_BASE_URL`.
