# Session handoff

_Last updated: 2026-09-24 on `refactor/v2-production-docs`._

## Current state

- Binding plan: [`instructions/qa-telegram-bot-refactor-v2-spec.md`](../instructions/qa-telegram-bot-refactor-v2-spec.md).
- Historical plan: `instructions/[deprecated]_pla_prototip_bot_telegram_bhc_v3.md`; it is not authoritative.
- Current branch: `refactor/v2-production-docs`.
- `main` is at the last merged production boundary commit; this branch is pushed but not merged.
- `TODO.md` contains an intentional uncommitted edit. Preserve it; do not stage, reset, or revert it.

## Merged work

1. PR #1 — correctness, spaces, connector provenance, semantic Q&A identity, stable vectors, projection manifest.
2. PR #2 — background evidence, bounded backlog, synthesis limits, reviewer authorization, atomic corrections.
3. PR #3 — Pydantic HTTP contracts, generic `/v1` API, durable delivery/interactions, scheduled report path, Telegram adapter split.
4. PR #4 — session handoff documentation.
5. PR #5 — local SQLite + NumPy + Ollama runtime, migrations, local Docker/Make workflow.
6. PR #6 — local environment aliases, readiness, bootstrap, and local E2E fixes.

## Implemented on the current branch

- Durable reviewer-delivery failure state and configurable admin escalation:
  - `REVIEWER_ESCALATION_TIMEOUT_SECONDS`;
  - timeout is shown in the notification;
  - admin can edit and approve after escalation;
  - test uses a zero-second timeout.
- Temporal listener pairing:
  - durable pending windows;
  - quiet-period flush;
  - configurable window/quiet/overlap settings;
  - stable candidate ids and idempotent evidence indexing;
  - local Ollama and Workers AI pairing adapters.
- Pair candidates remain non-authoritative evidence; no curator promotion is implemented.
- Deterministic daily report with addressed questions, background questions, ingestion, corrections, seed divergence, AI usage, and reviewer audit sections.
- D1 report source now calculates seed-divergence counts from refreshed web versions that did not replace human-approved current versions.
- Shared `AppContext` moved to `infrastructure/context.py`.
- Cloudflare settings now pass raw values through Pydantic instead of manual `_int/_float/_flag` coercion.

## Functional tests

Passing:

```text
make all
126 fast tests passed

make test-integration
124 integration tests passed

uv run python -m evals.run offline
6/6 suites passed

make test-e2e-local
2 passed
```

Important scenarios covered:

- real local Ollama embedding, generation, and pairing response validation;
- local seed → SQLite/NumPy retrieval → generic answer;
- correction proposal flow;
- two independent groups returning different corrected local answers;
- unreachable reviewer → admin timeout escalation → admin edit → local approval;
- mixed listener window accumulation and candidate indexing;
- deterministic daily report sections.

## Curator status

The official Q&A curator role is deliberately deferred. The current design only
stores and indexes `message_pair_candidates` as evidence. There is no curator
notification, candidate approval CLI, or automatic promotion to canonical Q&A.
Do not infer that the admin is permanently the curator.

## Remaining work

1. Finish Phase 10 structural cleanup:
   - split the oversized D1 module by concern;
   - make the Cloudflare composition location explicit;
   - remove obsolete recap/reviewer wiring where safe;
   - keep route context from exposing raw repositories.
2. Update final documentation and self-Q&A against the actual code.
3. Run final local, offline, Worker smoke, and authorized Cloudflare checks.
4. Do not run Cloudflare migrations, deploys, live AI evals, or remote reindexing
   without explicit human authorization.

## Handoff rule

Read `AGENTS.md` and the binding plan before continuing. Preserve the unstaged
`TODO.md` edit. The local Docker services and pulled Ollama models may still be
running; `make dev-down` preserves their volumes.
