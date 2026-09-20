# Deployment and setup

How to get from a clean checkout to a working bot, from zero.

This is a **Cloudflare Python Worker** with D1 (source of truth), Vectorize
(derived index) and Workers AI. It runs inside the free tier; there is no paid
fallback anywhere in the code.

---

## 1. Prerequisites

| Need | Why |
|---|---|
| Python 3.13 | runtime version |
| [`uv`](https://docs.astral.sh/uv/) | dependencies, env, running everything |
| Node/npm (`npx`) | `wrangler` for D1, secrets, Vectorize |
| A Cloudflare account | Workers, D1, Vectorize, Workers AI (free plan is enough) |
| A Telegram bot token | from [@BotFather](https://t.me/BotFather) |
| Docker (optional) | only for the fidelity check in `make smoke` |

---

## 2. Local checkout

```bash
git clone https://github.com/uladribia/qa-telegram-bot.git
cd qa-telegram-bot
uv sync
uv run --frozen pre-commit install --install-hooks   # optional, runs the gates on commit
cp .env.example .env
```

Fill in `.env` (see [configuration](#4-configuration)). `.env` is gitignored and
must never be committed.

Check the code is healthy before touching anything remote:

```bash
make all          # lint + fast tests
make test-all     # every tier
```

---

## 3. Cloudflare resources

Create the resources once. The names below match the committed
`wrangler.jsonc`; if you change them, change the file too.

```bash
npx wrangler login

# Source of truth
npx wrangler d1 create knowledge-bot

# Derived vector index
npx wrangler vectorize create knowledge-v1 --dimensions 768 --metric cosine
```

Copy the D1 `database_id` that `d1 create` prints into `wrangler.jsonc`.

Apply every migration **in order**:

```bash
for f in migrations/*.sql; do
  npx wrangler d1 execute knowledge-bot --remote --yes --file "$f"
done
```

The file-based form is flaky on a slow connection: if one fails with
`fetch failed`, re-run it or apply its statement with `--command`. Migrations are
not idempotent (they are plain `ALTER TABLE ADD COLUMN`), so check what has
already landed before re-applying:

```bash
npx wrangler d1 execute knowledge-bot --remote --yes --json \
  --command "SELECT name FROM pragma_table_info('qa_versions')"
```

Filtered retrieval needs **metadata indexes**, or queries return nothing:

```bash
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name kind --type string
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name status --type string
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name scope --type string
```

`scope` separates global knowledge from per-group knowledge; without it, scoped
queries return nothing.

---

## 4. Configuration

Non-secret values can live in `wrangler.jsonc` under `vars` (they ship with the
deploy). **Secrets must be set on the Worker**, never committed:

```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put INTERNAL_ADMIN_KEY
npx wrangler secret put ALLOWED_TELEGRAM_CHAT_IDS
npx wrangler secret put ADMIN_TELEGRAM_USER_ID
npx wrangler secret put ALLOWED_TELEGRAM_USER_IDS   # optional; comma-separated
```

Generate a webhook secret rather than inventing one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Every key is documented in [`.env.example`](../.env.example). The ones that bite
if wrong:

- `ALLOWED_TELEGRAM_CHAT_IDS` — comma-separated group chat ids, one per group
  the bot serves. Each must include the leading `-` for a group.
- `ADMIN_TELEGRAM_USER_ID` — only this user can approve corrections.
- `ALLOWED_TELEGRAM_USER_IDS` — who may open a **private chat** with the bot.
  The admin is always allowed. Leave it empty and only the admin can DM.

**Do not leave DMs open.** A Telegram bot username is public and discoverable, so
an unrestricted DM would let any stranger spend your shared free AI quota and send
the admin fake correction reviews. The bot ignores a DM from anyone who is neither
the admin nor on the allowlist — unless it replies to a prompt the bot itself
sent, which is how a group member proposes a correction.

Models are restricted to the zero-cost allowlist in code
(`ALLOWED_AI_MODELS`). Setting anything else raises a configuration error at
startup; that is deliberate.

---

## 5. Deploy

```bash
uv run pywrangler sync      # vendor dependencies for the Worker runtime
uv run pywrangler deploy
```

The command prints the URL and a `Current Version ID`. Check it is alive:

```bash
curl -s https://<your-worker>.workers.dev/healthz
# {"status":"ok"}
```

---

## 6. Register the webhook

Telegram must be told where to send updates. The secret must match
`TELEGRAM_WEBHOOK_SECRET`, or every request is rejected with 401.

```bash
BOT_BASE_URL=https://<your-worker>.workers.dev uv run kb set-webhook
```

To go back to polling or stop delivery:

```bash
uv run kb delete-webhook
```

---

## 7. First run

1. Add the bot to your Telegram group.
2. Find the group's chat id and add it to `ALLOWED_TELEGRAM_CHAT_IDS`. There is no
   `/chatid` command yet, so read it from the update:

   ```bash
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
     | python3 -m json.tool | grep -A2 '"chat"'
   ```

   Group ids are negative and must keep their leading `-`.
3. Register each group so knowledge can be scoped to it:

   ```bash
   BOT_BASE_URL=https://<worker>.workers.dev uv run kb group add --chat-id -1001234567890 --title "Prebenjamins"
   ```

4. Seed knowledge — see [knowledge-base.md](knowledge-base.md). The web seed is
   global; WhatsApp imports and other group-only sources take `--scope <chat-id>`.
5. Mention the bot and ask something. You should get an answer with sources.

---

## 8. Verifying a deploy

```bash
make test-all     # code, offline
make smoke        # Docker build + boot + /healthz
make eval-live    # live quality gate (costs AI quota — see operations.md)
```
