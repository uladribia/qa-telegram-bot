# Operations and maintenance

Keeping the bot alive, cheap, and honest.

---

## The AI quota guard

The single most important thing to understand. Workers AI on the free plan allows
**10,000 neurons per day**. When that runs out, **every model call fails** and the
bot can answer nothing until 00:00 UTC.

The account's real usage is not visible to a Worker, so the app meters it with a
character-based estimate (the `ai_budget` table, tuned by the `AI_*` settings). A
**25% reserve is held back for real user traffic**.

Consequences, by design:

| Path | When the budget is nearly gone |
|---|---|
| Evals, reindex, retrieval probes | **HTTP 429**, refused outright |
| A real user's question | still attempted; degrades to *"Ara mateix no puc consultar la informació"* |

A user question is never refused by the guard, so the inbound event is never lost.
The guard exists so a careless eval run cannot take the bot down for a day. It has
already happened once.

### Checking actual usage

The estimate is in D1; the truth is in the analytics API:

```bash
curl -s -X POST "https://api.cloudflare.com/client/v4/graphql" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "content-type: application/json" \
  -d '{"query":"query { viewer { accounts(filter: {accountTag: \"'$CLOUDFLARE_ACCOUNT_ID'\"}) { aiInferenceAdaptiveGroups(limit: 100, orderBy: [datetimeHour_ASC], filter: {datetime_geq: \"2026-01-01T00:00:00Z\"}) { sum { totalNeurons } dimensions { datetimeHour modelId } } } } }"'
```

The estimate is deliberately conservative: it may refuse evals while some budget
remains. That is the safe direction.

### Hard-won operational facts (2026-09-22)

- **The analytics API lags.** Hours after a heavy run it still reports less
  than was actually spent. Never calibrate the app's estimate from it while
  calls are still failing: on 2026-09-22 the API showed 2.6k neurons for the
  day while the model already answered *allocation exhausted* (the previous
  day had actually burned 10.7k). The in-app estimate tracked reality better
  than the analytics did.
- **The daily reset time is not confirmed to be 00:00 UTC.** After a day that
  exceeded the free allocation, the next day ran out long before midnight. If
  calls keep failing past 00:00 UTC, find the real reset hour with the GraphQL
  query above and re-run the eval gate manually once the quota is back.
- **A full reindex does not fit in one free day.** It embeds every active Q&A
  version and every message; the first run indexed 764 Q&A + 1,444 messages
  before the guard stopped it. The reindex is resumable by cursor (the CLI
  prints a `resume with: --qa-after ... --msg-after ...` hint) — run it in
  authorized, supervised batches over several nights, resuming from the hint.

### Simulating a spent day

```bash
npx wrangler d1 execute knowledge-bot --remote --yes \
  --command "INSERT INTO ai_budget (day, neurons, calls, updated_at) \
   VALUES ('2026-01-01', 20000, 1, '2026-01-01T00:00:00Z') \
   ON CONFLICT(day) DO UPDATE SET neurons=excluded.neurons"
```

---

## Reading the logs

```bash
npx wrangler tail <worker-name> --format pretty
```

**Known gap:** the app does not currently emit application logs — there are no
`logger.*` calls in the request paths, and `configure_logging()` runs in
human-readable mode rather than structured JSON. Cloudflare's own request logs
still work. Treat Worker logs as infrastructure-level, not application-level,
until this is addressed.

Privacy rules that must hold whenever logging is added: never log raw message
text, answers, sender names, usernames, phone numbers, raw payloads, or full
prompts. Log lengths, hashes, counts, similarities, model names and decisions.

---

## Health and smoke checks

```bash
curl -s https://<worker>.workers.dev/healthz      # {"status":"ok"}
make smoke                                        # Docker build + boot + /healthz
```

`/healthz` proves the Worker boots. It does **not** prove D1, Vectorize or the AI
binding work — a completely dead AI binding still returns 200.

---

## Common problems

| Symptom | Likely cause |
|---|---|
| Every AI route returns 500 | AI is unreachable — usually the daily quota. Check usage above. |
| Eval or reindex returns 429 | The quota guard. Working as intended; wait for 00:00 UTC. |
| Answers say *"no puc consultar la informació"* | Same root cause: the model call failed. |
| Retrieval returns nothing | Metadata indexes missing, or the base was never reindexed. |
| A reindex just ran but results look stale | Vectorize eventual consistency — wait ~60s. |
| A freshly approved correction still abstains | Same cause: the reindex runs on approval, but Vectorize needs up to ~a minute before the new vector is queryable. Re-ask after a short wait; see below. |
| Webhook returns 401 | `TELEGRAM_WEBHOOK_SECRET` and the registered secret disagree. |
| A correction is ignored | The confirmer is not `ADMIN_TELEGRAM_USER_ID`. |
| A stranger's DM gets no reply | Working as intended: only the admin and `ALLOWED_TELEGRAM_USER_IDS` may DM. |
| Citations show a phone number | The WhatsApp export had no saved contact name. |

---

## Routine maintenance

**After changing anything about embeddings, provenance or the index shape:**

```bash
make reindex
```

**After a deploy that touches routes, bindings or the Dockerfile:**

```bash
make smoke
```

**Daily-ish, when you care about answer quality:**

```bash
make eval-live            # measure only
make eval-live-reindex    # measure + rebuild first
```

**The live gate and `kb reindex` are quota-destructive operations.** One full
live eval is ~30-50 model calls (~1,500-3,000 neurons); a reindex attempt
burned 8,979 real neurons for 764 Q&A + 1,444 messages in a single hour
(2026-09-21). Rules:

- Run them **only with the user's explicit authorization**, one suite at a
  time, and only for **substantive changes that can affect answer quality**
  (a threshold change, a prompt change, a model change, a retrieval change) —
  never for curiosity, iteration, or scheduled maintenance.
- **Never retry a failed live eval automatically.** Every retry is a fresh
  full burn; six retries of a degraded-model run can spend a whole day's
  budget. Diagnose, then re-run manually.
- **Never schedule them.** No recurring or one-shot cron ever runs the live
  gate or the reindex: an unattended run cannot ask for authorization, and a
  failing one burns quota while nobody watches.
- A failed run still burns the calls it made before failing. The guard
  refusing with 429 *before* any call is the cheap failure — treat it as such,
  don't work around it.

The gate exits non-zero when a suite fails, with a one-line reason per failure.

---

## Approved corrections are not answerable instantly

When a correction is approved, the flow is: new version written to D1 → reindex
upserts it into Vectorize → the next equivalent question can be answered.
Vectorize is **eventually consistent**, so a vector can be written and processed
and still not appear in query results for up to about a minute. In that window a
re-ask of the corrected question abstains — the bot cannot see the new answer
yet, and abstaining is the honest response.

It self-heals: no action is needed, just re-ask. If a correction stays
unanswerable for more than a few minutes, force a reindex and check the index:

```bash
make reindex
npx wrangler vectorize info knowledge-v1
```

---

## Reverting a correction (CLI only)

There is **no rollback from Telegram**: a reviewer's approval cannot be undone
from the group or DMs, and the admin report is read-only. The only way back is
the CLI, which restores the version the correction superseded. Nothing is
deleted — the reverted version stays in the history:

```bash
BOT_BASE_URL=https://<worker>.workers.dev uv run kb revert <qa_item_id>
```

The item id is visible in the review report (`kb review`). The derived index is
updated as part of the revert; it can take up to a minute to settle.

---

## Migrations

Migrations live in `migrations/` and are applied **by hand**; there is no runner.

```bash
npx wrangler d1 execute knowledge-bot --remote --yes --file migrations/0007_ai_budget.sql
```

They are not idempotent (plain `ALTER TABLE ADD COLUMN`). If one fails with
`fetch failed`, check what landed before re-running:

```bash
npx wrangler d1 execute knowledge-bot --remote --yes --json \
  --command "SELECT name FROM pragma_table_info('qa_versions')"
```

---

## What is deliberately absent

No paid fallback, no alternative model provider, no queue, no auth layer, no web
frontend, no media processing, no scheduled cron. If a fix seems to need one of
these, stop and read the plan first.
