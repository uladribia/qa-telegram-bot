# Cloudflare setup

This is an explicit production/staging procedure. Commands that call Workers AI or mutate remote resources require `ALLOW_CLOUDFLARE_LIVE_TESTS=1` and an explicit `BOT_BASE_URL`.

1. Install the repository toolchain and authenticate Wrangler.
2. Create or select the v2 D1 database and Vectorize index.
3. Create Vectorize metadata indexes for `kind`, `status`, and `scope_key`.
4. Configure the D1, Vectorize, and AI bindings in `wrangler.jsonc`. The Worker loads the linear classifier head from the generated module `src/knowledge_bot/infrastructure/classifier_head_data.py` (a Python Worker isolate cannot read repository-relative data files); that module is regenerated from `data/classifier/model.json` by `uv run python scripts/train_classifier.py`.
5. Set secrets with `wrangler secret put`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ADMIN_TELEGRAM_USER_ID`, and `INTERNAL_ADMIN_KEY`. Add `LOGFIRE_TOKEN` (a send-only Logfire write token) to enable production traces; without it the Worker starts normally and keeps its spans local. Never put it in `wrangler.jsonc`.
6. Apply shared migrations in numeric order. The derived FTS5 projection is created by `0021_search_fts.sql` and dropped again by `0022_drop_search_fts.sql`; applying both is a no-op on the table.
7. Deploy the Worker with `make deploy` (`ALLOW_CLOUDFLARE_LIVE_TESTS=1`). It runs `pywrangler`, which vendors the Python dependencies into `src/vendor` before building; a bare `npx wrangler deploy` ships a Worker with no vendored code.
8. Check `/healthz` and `/readyz` where applicable. `/healthz` is served by the static asset layer (`public/healthz`, deployed with `run_worker_first: false`) and answers from the edge even if the Python interpreter cannot start; `/readyz` runs inside Python and is the application-side probe. Monitor both. Neither resolves the application context, so neither exercises telemetry: after any observability change, POST a webhook and read the logs.
9. Seed committed data and register logical spaces/channel bindings.
10. Register the Telegram webhook at `/adapters/telegram/webhook` (`make set-webhook` does this for you). The path changed with the adapter split in v1.2.0; re-register after deploying, or Telegram keeps posting to the old URL and the bot goes silent. Deploy first — repointing Telegram at a route the Worker does not serve yet is a `404` and a silent bot.
11. Run the guarded tiny smoke only after explicit authorization:

```bash
ALLOW_CLOUDFLARE_LIVE_TESTS=1 \
BOT_BASE_URL=https://<worker> \
make smoke-cloudflare
```

The smoke performs one embedding and at most one generation call, writes a temporary vector, and removes it in `finally`. It does not run a full eval or full reindex.

Live evals are protected against load, not just quota: `/internal/retrieve` accepts at most 20 queries per request, and each isolate admits 60 evaluation calls per minute (`429` after that). Run the suites one at a time with `--suite`; a full retrieval run is 500 queries, so it is sent as paced chunks.

There is no `ALLOWED_TELEGRAM_CHAT_IDS` setting. A group is served only after its Telegram conversation is bound to a logical space.

## Decisions in production

The Worker decides listener intent, answer sufficiency and evidence selection
with `@cf/cloudflare/clef-flash` through the existing `AI` binding; no new binding
and no new secret. `DECISION_BACKEND` defaults to `systemone`, so a fresh deploy
needs nothing extra. `baseline` removes both decision calls and is the rollback.

The decision model is metered like every other Workers AI call, against the same
daily budget, so it appears in `GET /internal/budget`. It is small: roughly three
neurons per listener message and twenty-five per answered question, against
about a hundred for a generation. The cosine floor and `QA_TOP_K` /
`MESSAGE_TOP_K` still bound what may reach it, so the model cannot widen the
bill.

After deploying, send one evaluation case to confirm the binding answers before
trusting the listener:

```bash
curl -sS -X POST "$BOT_BASE_URL/internal/eval/decision" \
  -H "X-Internal-Key: $INTERNAL_ADMIN_KEY" -H 'content-type: application/json' \
  -d '{"backend":"clef-flash","cases":[{"case_id":"probe","text":"Quan entrenen?","candidates":[]}]}'
```

See [operations.md](operations.md#decisions-and-rollback) for troubleshooting and
rollback.
