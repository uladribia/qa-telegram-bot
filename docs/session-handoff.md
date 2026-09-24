# Session handoff

_Last updated: 2026-09-24 after PR #3._

## Repository state

- Branch used for this handoff: `docs/session-handoff`.
- Base commit: `d0577e6` (`main` after PR #3).
- Binding plan: [`instructions/qa-telegram-bot-refactor-v2-spec.md`](../instructions/qa-telegram-bot-refactor-v2-spec.md).
- Historical plan: `instructions/[deprecated]_pla_prototip_bot_telegram_bhc_v3.md`; it is not authoritative.
- `main` and `origin/main` were synchronized when PR #3 merged.
- `TODO.md` contains an intentional, uncommitted local edit. Preserve it; do not
  stage, revert, or overwrite it during the next implementation branch.

## Merged PRs

1. **PR #1 — `refactor/v2-correctness`**
   - https://github.com/uladribia/qa-telegram-bot/pull/1
   - merge: `8618265`
   - immediate correctness, spaces, connector-defined source provenance, semantic
     Q&A identity, stable Q&A vector IDs, projection manifest.

2. **PR #2 — `refactor/v2-learning-review`**
   - https://github.com/uladribia/qa-telegram-bot/pull/2
   - merge: `33e1bc0`
   - evidence-only background indexing, bounded backlog processing, lower-priority
     AI budget, synthesis limits, no runtime judge, scoped reviewer authority,
     pending unreachable reviews, atomic correction commit.

3. **PR #3 — `refactor/v2-boundaries-report`**
   - https://github.com/uladribia/qa-telegram-bot/pull/3
   - merge: `d0577e6`
   - Pydantic HTTP contracts, generic `/v1/*` API, idempotent question requests,
     durable delivery receipts, consumed-once Telegram interactions, daily report
     state, Worker scheduled handler, Cron Trigger, Telegram webhook adapter split.

## Current implemented behavior

- SQL is authoritative; Vectorize is a derived projection.
- `data/` is the durable knowledge input. Existing D1 experiment data is
  disposable and the canonical cutover may use fresh D1/Vectorize resources.
- Connectors declare `SourceDescriptor(id, kind, authority)`. Core code has no
  Telegram/WhatsApp/web source registry; an architecture test enforces this.
- Logical spaces and channel bindings are independent from Telegram chat IDs.
- Q&A identity is semantic; source anchors stay on versions.
- Q&A vectors use stable `qa:<qa_item_id>` IDs and `search_projection` tracks the
  derived state.
- Background messages are stored even when classification/indexing is skipped.
  Only eligible evidence is indexed; questions and chitchat are not factual
  evidence.
- Deferred background work requires an explicit bounded maintenance request.
- Corrections use one SQL transaction for version, pointer, evidence, and feedback
  state. A failed D1 batch rolls back all writes.
- Reviewer authorization checks actor and requested approval scope. A local
  reviewer cannot approve global knowledge.
- Generic application endpoints exist under `/v1` and do not know Telegram.
- Telegram prompt/reply interactions and answer deliveries are durable SQL state.
- A deterministic daily report job is available at
  `POST /internal/jobs/daily-report` and from the Worker Cron handler. The full
  report-content work from Phase 9 is not finished; current recap/reviewer report
  services still supply the interim content.

## Validation baseline

Run before changing the next branch:

```bash
make lint
make test
make test-integration
uv run python -m evals.run offline
```

Expected at the handoff baseline:

- lint/type checks: pass;
- fast tests: 124 passed;
- integration tests: 115 passed;
- offline eval suites: 6/6 passed;
- `make smoke`: passed (`/healthz` returned OK).

`make smoke` was last run after the generic API, durable state, daily report,
Worker entrypoint, and Telegram adapter changes.

## Next branch and scope

Create from the updated `main`:

```bash
git switch -c refactor/v2-local-runtime
```

Implement Phase 8 only first, keeping the branch green:

1. Add the approved local dependencies (`numpy`, `aiosqlite`, `uvicorn`; use the
   existing `httpx` development dependency for Ollama HTTP).
2. Add `migrations/local/0001_local_vectors.sql` and a tiny local migration runner
   using `schema_migrations`; do not add Alembic.
3. Implement SQLite repositories and the `NumpySqliteVectorStore` with the
   `local_vectors` schema and strict vector dimensions.
4. Implement local Ollama embedder/generator through `/api/embed` and
   `/api/chat`; validate Pydantic output and retry invalid generated JSON once.
5. Add `RuntimeMode`, local settings/composition, and `local_entry.py`.
6. Add the canonical Docker/Make targets and `.env.local.example` from the plan.
7. Add local AI E2E tests using a real local Ollama only when explicitly running
   `make test-e2e-local`; never make fast tests call Ollama or Cloudflare.
8. Update `README.md`, local setup docs, usage/operations docs, and the bot
   self-Q&A before merging this branch.

## Known transitional gaps

These are intentional follow-up work, not reasons to undo completed PRs:

- The canonical local SQLite/NumPy/Ollama runtime does not exist yet.
- `wrangler.jsonc` and the Cloudflare composition are still the only deployed
  runtime; the Worker scheduled handler exists but the full Phase 9 report
  sections are not complete.
- The large HTTP app still contains legacy operational and Telegram helper
  routing; the generic API and Telegram webhook registration are split, but
  Phase 10 may split the remaining internal route modules.
- Telegram reviewer persistence still has legacy raw-id compatibility fields;
  generic API flows use principals, but complete reviewer-principal migration is
  still worth checking before final acceptance.
- Generic idempotent question replay returns the stored answer and answer id;
  verify whether the API contract should replay structured sources as well.
- No Cloudflare live migration, deploy, D1 reseed, Vectorize metadata-index
  change, or live AI evaluation was authorized or run in this session. Do not
  run those without explicit human authorization.
- `TODO.md` is modified locally and must survive the handoff.

## Handoff rule

Before starting Phase 8, read the binding plan and `AGENTS.md`, inspect
`git status`, and preserve the unstaged `TODO.md` edit. Do not reset the worktree
or use destructive checkout commands to discard it.
