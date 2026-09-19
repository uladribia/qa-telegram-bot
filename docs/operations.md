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
| Webhook returns 401 | `TELEGRAM_WEBHOOK_SECRET` and the registered secret disagree. |
| A correction is ignored | The confirmer is not `ADMIN_TELEGRAM_USER_ID`. |
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

Run the live gate **at most once or twice a day** — it is the largest quota
consumer. It exits non-zero when a suite fails, with a one-line reason per failure.

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
