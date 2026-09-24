# Architecture

The Worker has one SQL source of truth: D1 in production and SQLite locally. The vector index is a derived, rebuildable projection.

## Runtime flow

```text
Telegram webhook
  -> Telegram adapter
  -> normalized channel-independent message
  -> durable ingestion
  -> application services
  -> SQL semantic writes
  -> durable vector projection
  -> adapter delivery
```

A Telegram group is served only when `(telegram, chat_id)` has an active `channel_bindings` row. Private Telegram DMs remain separately allowlisted. The bot does not use a second group allowlist.

## Knowledge model

Knowledge has two scopes: `global` and `space:<space_id>`. A local Q&A item suppresses only the global item with the same canonical question. A local correction does not change another space.

Q&A items have stable vector ids `qa:<qa_item_id>`. Message evidence has stable vector ids `msg:<message_id>`. Temporal question-answer candidates are audit rows only; accepted pairs become ordinary message evidence under the answer message id.

## Roles

Reviewers are identified by opaque `principal_id` values. A local reviewer can review only its own space and cannot approve global knowledge. A global reviewer or admin can approve local or global scope. The admin is configured as `telegram:<ADMIN_TELEGRAM_USER_ID>` at composition time.

## AI and quota

Direct questions are never rejected by the estimate guard. Background and maintenance work check admission before each AI-consuming unit. Production Workers AI calls have hard adapter deadlines. Production grounded generation makes one structured call; malformed output becomes an abstention.

## Repair

SQL semantic commits are not rolled back when a derived projection fails. The projection manifest records `pending`, `active`, or `failed`, and `kb index repair --limit 100` repairs a bounded batch. A full rebuild uses a separate zero-AI cleanup followed by bounded batches; an empty reindex request never starts a destructive rebuild.
