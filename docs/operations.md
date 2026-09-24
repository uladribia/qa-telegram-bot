# Operations and maintenance

Keeping the bot alive, cheap, and honest.

---

## The AI quota guard

The single most important thing to understand. Workers AI on the free plan allows
**10,000 neurons per day**. When that runs out, **every model call fails** and the
bot can answer nothing until 00:00 UTC.

The account's real usage is not visible to a Worker, so the app meters it with a
character-based estimate (the `ai_budget` table, tuned by the `AI_*` settings). A
**25% reserve is held back for real user traffic**. Background classification
stops at 50% estimated spend (`AI_BACKGROUND_BUDGET_FRACTION`); maintenance
stops at 70% (`AI_MAINTENANCE_BUDGET_FRACTION`). Deferred background messages
remain stored and are not retried automatically.

Consequences, by design:

| Path | When the budget is nearly gone |
|---|---|
| Evals, reindex, retrieval probes | **HTTP 429**, refused outright |
| Background classification | stored as `deferred_budget`; no embedding call |
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

## Case study: the "silent bot" of 2026-09-23 (model latency, not code)

The bot stopped replying while every structural check passed. Diagnosis from
that day, kept here because the pattern will recur: any AI-side degradation
longer than Telegram's webhook timeout makes the bot look dead with zero local
evidence.

### What was observed

- `/healthz` returned 200, and the webhook answered a signed probe in 1.3s.
  Delivery, secret auth, D1 and routing were all fine.
- `getWebhookInfo` showed `last_error: "Read timeout expired"`: Telegram calls
  the webhook, the Worker does not answer within ~60s, Telegram cancels and the
  user sees nothing. Cloudflare request logs show `outcome: "canceled"` with
  almost no CPU time — the Worker is parked awaiting the model, not burning CPU.
- `/internal/eval/answer` (same pipeline without Telegram) took **31s to 236s**
  per call and sometimes ended in `ModelUnavailableError` → `mode:
  "unavailable"`. The very same prompt + evidence via the REST endpoint
  (`/accounts/<id>/ai/run/...`) answered in ~20s.

### Root cause

The **AI binding call to `@cf/zai-org/glm-4.7-flash`** was degraded
(erratic latency up to minutes, occasional failures). Everything around it was
verified independently and healthy: the embedding model (including the
classifier's 17-text batch), Vectorize queries, D1, and the same model over
REST. The outage window was bounded by the model call: as soon as a generation
finished inside ~45s the bot answered normally.

### How to diagnose the same pattern

1. `curl getWebhookInfo` — `Read timeout expired` with low `pending_update_count`
   means updates arrive but the Worker is too slow to answer them.
2. `POST /internal/eval/answer` with a generous timeout (`-m 300`). A fast
   response rules the pipeline out; minutes or `mode: "unavailable"` point at
   the model binding.
3. Reproduce the generation call over REST with the same model and evidence.
   REST fast + binding slow ⇒ degradation is binding/model-side, not a code
   change. Check the Cloudflare status page for Workers AI incidents.
4. `npx wrangler tail <worker> --format json` shows `outcome: "canceled"` with
   tiny `cpuTime` — the request was waiting on I/O when the client left.

### What was deliberately NOT treated as the cause

The classifier was suspected first (it is the newest component). It is not in
the answering path (`dry_run` → retrieve → decide never classifies), and its
batched embed worked in 1.3s during the outage. The classifier has known
quality weaknesses (module-global prototype cache, thresholds tuned on 4
prototypes per label) but was innocent here.

### Open mitigation (not yet done)

A single slow generation currently holds the webhook for minutes before the
binding itself errors. A per-call timeout at the adapter boundary (e.g.
`asyncio.wait_for` in `WorkersAIGenerator._run`) would convert that into the
planned fast degradation (*"Ara mateix no puc consultar la informació"*
sent within the webhook window) instead of a silent cancellation by Telegram.

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
| Retrieval returns nothing | Metadata indexes `kind`, `status`, and `scope_key` are missing, or the base was never reindexed. |
| A reindex just ran but results look stale | Vectorize eventual consistency — wait ~60s. |
| A freshly approved correction still abstains | Same cause: the reindex runs on approval, but Vectorize needs up to ~a minute before the new vector is queryable. Re-ask after a short wait; see below. |
| Webhook returns 401 | `TELEGRAM_WEBHOOK_SECRET` and the registered secret disagree. |
| A correction is ignored | The confirmer is not `ADMIN_TELEGRAM_USER_ID`. |
| A stranger's DM gets no reply | Working as intended: only the admin and `ALLOWED_TELEGRAM_USER_IDS` may DM. |
| Bot ignores everyone, but `/healthz`, the webhook and D1 are fine | AI binding latency above Telegram's webhook timeout — see the 2026-09-23 case study above. |
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

### Authorizing a full reindex

A full reindex is for **substantial knowledge-base changes only**: the initial
WhatsApp import, a scope or metadata change, an embedding-model change, or a
suspected stale index. Routine additions do not need it — seeding indexes new
and renewed Q&A incrementally (~4 neurons each), and approved corrections
reindex their single version automatically.

When a full rebuild is genuinely needed, ask the user first, with the budget
stated up front:

1. Count the records the rebuild will touch:

   ```bash
   npx wrangler d1 execute knowledge-bot --remote --yes --command \
     "SELECT (SELECT COUNT(*) FROM qa_items qi JOIN qa_versions qv ON qi.current_version_id = qv.id WHERE qi.status = 'active') AS qa, (SELECT COUNT(*) FROM messages WHERE text IS NOT NULL AND text != '') AS messages"
   ```

2. Report the estimate: **~4 real neurons per record** (measured: 8,979
   neurons for 2,208 records on 2026-09-21), so a 2,000-record base is most of
   a free day's budget. The in-app guard estimate runs ~4× higher and trips
   first — that is expected.
3. Wait for the user's explicit yes. If it cannot fit in the remaining day,
  propose batching over several nights with the resume cursors instead.

The gate exits non-zero when a suite fails, with a one-line reason per failure.

---

## Approved corrections are not answerable instantly

When a correction is approved, the flow is: new version written to D1 → the
stable `qa:<qa_item_id>` vector is refreshed in Vectorize → the next equivalent
question can be answered. The previous version id is never used as a second
searchable vector.
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
