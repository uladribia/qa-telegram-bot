# Architecture

The Worker has one SQL source of truth: D1 in production and SQLite locally. The vector index is a derived, rebuildable projection.

## Layers

```text
api/          FastAPI: the app factory, the canonical /v1 contract, internal operator routes
adapters/     connectors: Telegram (webhook, identity, payloads, delivery), importers
models/       DTOs shared across channels; connector payloads stay with their adapter
application/  use cases, wired to ports
domain/       entities, policies, enums
ports/        protocol interfaces
infrastructure/ concrete externals: D1, Vectorize, Workers AI, logging, settings
```

`api/` owns FastAPI and is not a channel adapter. The canonical `/v1` routes and
the Telegram webhook are two front doors onto the same application services;
neither calls the other over HTTP. A connector owns everything channel-specific,
including its payload models, so nothing outside it imports a Telegram type.
`tests/architecture/test_layer_boundaries.py` fails if a canonical route starts
importing a connector.

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

A Telegram group is served only when `(telegram, chat_id)` has an active `channel_bindings` row. The row also carries the `bot_mode` that decides how much the bot does in that conversation (`off`, `silent`, `active`, `proactive`): the mode is a property of the binding because it is the bot's behaviour there, not the extent of the space's knowledge. Private Telegram DMs have their own mode, `TELEGRAM_DM_BOT_MODE`. There is no second group allowlist.

The webhook applies the mode only after the control plane. Resolving the binding, recording who was seen in it, and the reviewer and correction prompts are never gated by it: the mode decides what happens to a question, never whether the bot keeps a promise it already made.

## Membership

`space_memberships` records that a principal was observed in a space, with the first and last time it was seen. Membership is observation, not enumeration: the bot has no admin rights, does not call `getChatMember`, and does not backfill, so anyone it has never seen in a served group is unknown to it. An ordinary group message marks its human sender as a member; a press of one of the bot's buttons in a served group marks the person who pressed it; Telegram's join and leave service messages mark the people who arrived and departed. Bots are never members, and the person who removes someone is not evidence about themselves.

A private message is authorized by that observation, or by the admin/allowlist override. `DirectAnswerService` then answers it once per served group the asker belongs to, collapsing answers that are identical in text and sources, and labelling each surviving block with the scopes of the evidence behind it. That label is read from the projection metadata, so it reports what the answer is made of rather than which scope was searched.

## Knowledge model

Knowledge has two scopes: `global` and `space:<space_id>`. A local Q&A item suppresses only the global item with the same canonical question. A local correction does not change another space. Both facts matter for a private multi-scope answer: a group round that finds a local record therefore does not also find the global one it overrides, and the two scopes' answers are then visibly different rather than accidentally identical.

Q&A items have stable vector ids `qa:<qa_item_id>`. Message evidence has stable vector ids `msg:<message_id>`. Temporal question-answer candidates are audit rows only; accepted pairs become ordinary message evidence under the answer message id.

## Roles

Reviewers are identified by opaque `principal_id` values. A local reviewer can review only its own space and cannot approve global knowledge. A global reviewer or admin can approve local or global scope. The admin is configured as `telegram:<ADMIN_TELEGRAM_USER_ID>` at composition time.

## AI and quota

Direct questions are never rejected by the estimate guard. Background, proactive, and maintenance work check admission before each AI-consuming unit, in that order of how optional they are: uninvited answers stop at `AI_PROACTIVE_BUDGET_FRACTION`, before the background classification and indexing that make tomorrow's answers possible. Production Workers AI calls have hard adapter deadlines. Production grounded generation makes one structured call; malformed output is not treated as a model abstention but recorded as its own reason, `invalid_model_output`, with a safe parse code.

## Repair

SQL semantic commits are not rolled back when a derived projection fails. The projection manifest records `pending`, `active`, or `failed`, and `kb index repair --limit 100` repairs a bounded batch. A full rebuild uses a separate zero-AI cleanup followed by bounded batches; an empty reindex request never starts a destructive rebuild.
