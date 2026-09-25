# Cloudflare setup

This is an explicit production/staging procedure. Commands that call Workers AI or mutate remote resources require `ALLOW_CLOUDFLARE_LIVE_TESTS=1` and an explicit `BOT_BASE_URL`.

1. Install the repository toolchain and authenticate Wrangler.
2. Create or select the v2 D1 database and Vectorize index.
3. Create Vectorize metadata indexes for `kind`, `status`, and `scope_key`.
4. Configure the D1, Vectorize, and AI bindings in `wrangler.jsonc`. The Worker loads the linear classifier head from the generated module `src/knowledge_bot/infrastructure/classifier_head_data.py` (a Python Worker isolate cannot read repository-relative data files); that module is regenerated from `data/classifier/model.json` by `uv run python scripts/train_classifier.py`.
5. Set secrets with `wrangler secret put`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ADMIN_TELEGRAM_USER_ID`, and `INTERNAL_ADMIN_KEY`.
6. Apply shared migrations in lexical order, including `0021_search_fts.sql` (the derived BM25/FTS5 lexical projection).
7. Deploy the Worker.
8. Check `/healthz` and `/readyz` where applicable.
9. Seed committed data and register logical spaces/channel bindings.
10. Register the Telegram webhook at `/telegram/webhook`.
11. Run the guarded tiny smoke only after explicit authorization:

```bash
ALLOW_CLOUDFLARE_LIVE_TESTS=1 \
BOT_BASE_URL=https://<worker> \
make smoke-cloudflare
```

The smoke performs one embedding and at most one generation call, writes a temporary vector, and removes it in `finally`. It does not run a full eval or full reindex.

There is no `ALLOWED_TELEGRAM_CHAT_IDS` setting. A group is served only after its Telegram conversation is bound to a logical space.
