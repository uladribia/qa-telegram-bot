# Session handoff

_Last updated: 2026-09-24 on `docs/session-handoff-e2e-status`._

## Current state

- Main was merged and pushed at `f263ff7` with the final hardening pass.
- Current branch: `docs/session-handoff-e2e-status`.
- The working tree also contains an uncommitted replacement retrieval/classifier/listener plan under `instructions/`; preserve it and do not stage it with this documentation-only change.
- `AGENTS.md` still needs a separate update to point at the replacement plan if that plan becomes the active contract.

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
make all                  # 123 fast tests passed
make test-integration     # 120 integration tests passed
make test-all              # 243 passed, 4 skipped
uv run python -m evals.run offline  # 9/9 suites
make dev-bootstrap
make test-e2e-local       # 4 local E2E tests passed
make smoke                # local /healthz passed
```

`make test-e2e-local` covered the real local Ollama smoke, the existing local seed/retrieval flow, the synthetic Telegram two-space correction flow, and the local background temporal-pair flow. It did **not** cover the restart-persistence scenario. No real Telegram acceptance, Cloudflare smoke, live eval, remote reindex, or production mutation was performed.

## Remaining acceptance

- Add and run the restart-persistence scenario against the same local SQLite database.
- Complete the human-operated real Telegram two-group acceptance flow in [`docs/e2e-telegram.md`](e2e-telegram.md).
- Run Cloudflare smoke or live evaluation only after explicit human authorization and an explicit `BOT_BASE_URL`.
