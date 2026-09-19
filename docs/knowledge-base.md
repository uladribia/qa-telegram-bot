# Updating the knowledge base

Where knowledge comes from, and how to add or change it.

**D1 is the source of truth.** Vectorize is a derived index and can be rebuilt at
any time with `make reindex`. Never treat the index as the database.

---

## The three sources

| Source | Authority | Added by |
|---|---|---|
| Website Q&A snapshot | highest for facts | `kb snapshot-web` + `kb seed` |
| WhatsApp export | medium (conversation history) | `kb import-whatsapp` + `kb seed` |
| Group messages | low, and only when listening | automatic, or the importer |
| Approved corrections | highest | the correction flow in Telegram |

A correction always outranks the original. Originals are never edited: the web
Q&A that shipped is still there, byte for byte.

---

## Adding knowledge from a web page

Parse the page into a JSON file, then push it to the deployed Worker.

```bash
# 1. Snapshot the Q&A page into data/seed/qa.json
uv run kb snapshot-web --url "https://example.org/faq" --out data/seed/qa.json

# 2. Push it into D1 through the Worker
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --qa data/seed/qa.json

# 3. Rebuild the derived index
make reindex
```

The parser is generic, not tied to one site. It is configured for the headings and
answer shapes the page uses; if a different page yields too few entries it refuses
to write rather than seeding nonsense (the floor is 30 entries).

**Then**: `make reindex`, or the new knowledge is invisible to retrieval.

The parser stores the **exact anchored URL** for each entry, which is what
citations show.

---

## Adding knowledge from a WhatsApp export

```bash
uv run kb import-whatsapp data/raw/whatsapp.txt --out data/seed/whatsapp.jsonl
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --messages data/seed/whatsapp.jsonl
make reindex
```

The parser handles iOS one-digit dates and 12-hour AM/PM timestamps. Authors are
stored as they appear in the export, so a contact saved without a name shows up as
a phone number in citations — that is the source data, not a bug.

Seeding is **idempotent**: re-running skips what is already there. To re-import
after a parser change, delete the rows first (there is no compatibility path):

```bash
npx wrangler d1 execute knowledge-bot --remote --yes \
  --command "DELETE FROM messages WHERE source_id='whatsapp_import'"
```

---

## Adding knowledge by talking to the bot

With `BACKGROUND_LISTENER_ENABLED=true`, unaddressed group messages are stored as
low-authority knowledge. The bot still does not answer them. This is how the base
grows from real conversation.

Keep it **off** unless you want that: it stores everything said in the group.

---

## Correcting knowledge from Telegram

The normal path, and the only one that produces a human-approved answer:

1. Someone presses `⚠️ Està malament?` on a wrong answer.
2. They propose the correction in a private chat.
3. The admin gets it privately and approves / edits / rejects.

An approval creates a **new version** with the proposer's name and the proposal
date, and supersedes the old one. Nothing is overwritten, so every answer stays
auditable back to its source.

---

## Data rules

- **Never commit data.** `data/` is gitignored; `data/raw/*` and `data/seed/` are
  not in the repository.
- Tests use synthetic fixtures only, never a real export.
- Seeding goes through `POST /internal/seed`, not a committed SQL file, so no data
  ever enters git history.

---

## Reindexing

```bash
make reindex                       # or: uv run kb reindex
```

`make reindex` reads every active Q&A version and every message with text,
re-embeds them, and upserts into Vectorize with the metadata retrieval filters on.

Two things to expect:

- It **costs AI quota**. On the free plan 10k neurons/day, a full rebuild is a
  large bite — don't run it casually. See [operations.md](operations.md).
- Vectorize is **eventually consistent**. Right after a reindex, queries can read
  stale metadata for about a minute. If results look impossible, wait and retry
  before believing them.
