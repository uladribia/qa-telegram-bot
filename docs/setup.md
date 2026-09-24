# Deployment and setup

How to get from a clean checkout to a working bot, from zero.

The canonical development runtime is **local SQLite + NumPy + Ollama**. Production
uses a **Cloudflare Python Worker** with D1 (source of truth), Vectorize
(derived index) and Workers AI. Both runtimes are zero-cost; there is no paid
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

## 2. Start the canonical local runtime

Requirements: Docker, curl, and `uv`. No Cloudflare account, D1 database,
Vectorize index, Workers AI token, or Node/Wrangler installation is needed.

```bash
git clone https://github.com/uladribia/qa-telegram-bot.git
cd qa-telegram-bot
cp .env.local.example .env.local
make dev-bootstrap
```

`dev-bootstrap` is idempotent. It creates the `knowledge-bot-dev` network and
volumes, starts the pinned `ollama/ollama:0.11.10` container, pulls
`embeddinggemma` and `gemma3:270m`, builds the local app image, applies shared
and local SQLite migrations, and starts the app on port 8000.

```bash
curl -s http://localhost:8000/healthz  # {"status":"ok"}
curl -s http://localhost:8000/readyz   # checks SQLite, local_vectors, Ollama, models
make dev-logs
```

The local SQLite file lives in the `knowledge-bot-data` Docker volume. Stop
containers without deleting data using `make dev-down`. `make dev-reset` is
destructive and requires `CONFIRM=1`:

```bash
make dev-reset CONFIRM=1
```

Run the explicit local model smoke test only after the local services are ready:

```bash
make test-e2e-local
```

The ordinary `make test` and `make test-integration` tiers never call Ollama or
Cloudflare. Local model defaults are validated against
`LOCAL_ALLOWED_AI_MODELS`; Cloudflare settings are validated separately.

## 3. Local checkout for offline development

Use this path when developing without the Docker runtime:

```bash
uv sync
cp .env.example .env
make all
```

The source checkout, tests, and offline evals do not need any external service.

## 4. Cloudflare checkout

```bash
git clone https://github.com/uladribia/qa-telegram-bot.git
cd qa-telegram-bot
uv sync
uv run --frozen pre-commit install --install-hooks   # optional, runs the gates on commit
cp .env.example .env
```

Fill in `.env` (see [configuration](#5-configuration)). `.env` is gitignored and
must never be committed.

Check the code is healthy before touching anything remote:

```bash
make all          # lint + fast tests
make test-all     # every tier
```

---

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
npx wrangler vectorize create-metadata-index knowledge-v1 --property-name scope_key --type string
```

`scope` separates global knowledge from per-group knowledge; without it, scoped
queries return nothing.

---

## 5. Configuration

Non-secret values can live in `wrangler.jsonc` under `vars` (they ship with the
deploy). **Secrets must be set on the Worker**, never committed:

```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put INTERNAL_ADMIN_KEY
npx wrangler secret put ALLOWED_TELEGRAM_CHAT_IDS
npx wrangler secret put ADMIN_TELEGRAM_USER_ID
npx wrangler secret put ALLOWED_TELEGRAM_USER_IDS   # optional; comma-separated
# Identity values that used to live in wrangler.jsonc vars; kept as secrets so
# the public repo does not name the live bot:
npx wrangler secret put TELEGRAM_BOT_ID
npx wrangler secret put TELEGRAM_BOT_USERNAME
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

## 6. Deploy

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

## 7. Register the webhook

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

## 8. First run

1. Add the bot to your Telegram group.
2. Find the group's chat id and add it to `ALLOWED_TELEGRAM_CHAT_IDS`. There is no
   `/chatid` command yet, so read it from the update:

   ```bash
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
     | python3 -m json.tool | grep -A2 '"chat"'
   ```

   Group ids are negative and must keep their leading `-`.
3. Register each group so its Telegram conversation is durably bound to a
   logical space. Both this binding and `ALLOWED_TELEGRAM_CHAT_IDS` are required:

   ```bash
   BOT_BASE_URL=https://<worker>.workers.dev uv run kb group add --chat-id -1001234567890 --title "Prebenjamins"
   ```

4. Seed knowledge — see [knowledge-base.md](knowledge-base.md). The web seed is
   global; WhatsApp imports and other group-only sources take `--scope <chat-id>`.
5. Mention the bot and ask something. You should get an answer with sources.

---

## 9. Verifying a deploy

```bash
make test-all     # code, offline
make smoke        # Docker build + boot + /healthz
make eval-live    # live quality gate (costs AI quota — see operations.md)
```
