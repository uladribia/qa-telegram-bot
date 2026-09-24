# Operations

## Local versus Cloudflare

A synthetic local Telegram failure indicates application or adapter logic. A synthetic test that passes locally but fails against a real Telegram API indicates connector configuration or API behavior. A local real-Telegram flow that works but Cloudflare fails indicates D1, Vectorize, Workers AI, or Worker adapter/runtime behavior.

## Quota

Direct user questions are never refused by the estimate guard. Background classification and maintenance work are lower priority and check admission before each AI-consuming unit. Live evals, remote reindexing, and Cloudflare smoke require explicit authorization and an explicit `BOT_BASE_URL`.

Do not schedule live evals or automatically retry a failed remote operation. A full reindex is not a routine repair.

## Answer delivery

The inbound row and answer are persisted before Telegram delivery. A Telegram `ok=false` response is a delivery failure and produces a retryable webhook response. A delivery receipt prevents intentional duplicate sends after success; a crash after Telegram accepts a message but before receipt persistence can still produce a duplicate on retry.

## Projection repair

Projection state is durable:

- `pending`: reserved but not active;
- `active`: current successful projection;
- `failed`: projection needs repair.

Run a bounded repair first:

```bash
uv run kb index repair --limit 100
```

Use a full rebuild only when SQL truth is known to be correct and the derived index needs complete reconstruction. Rebuild cleanup removes manifest-known, legacy Q&A, legacy raw-message, and legacy `pair:*` ids before reindexing current SQL records.

## Logs

Local logs are human-readable. Cloudflare logs are structured JSON. Logs contain ids, scope, decisions, counts, model names, durations, and state transitions. They never contain raw message text, answers, prompts, usernames, phone numbers, tokens, or secrets.

## Daily report

The deterministic daily report is sent by the scheduled Worker handler and can be run manually with `POST /internal/jobs/daily-report`. `dry_run=true` renders the same report without sending or updating the last-sent timestamp. Legacy recap and reviewer-report routes are retired.
