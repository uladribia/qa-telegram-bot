# Updating the knowledge base

Where knowledge comes from, and how to add or change it.

**D1 is the source of truth.** Vectorize is a derived index and can be rebuilt at
any time with `make reindex`. Never treat the index as the database.

---

## Knowledge scopes

Knowledge lives at one of two levels:

- **`global`** — visible from every group (the shared knowledge base).
- **a group chat id** (e.g. `-1001234…`) — visible only when the question is
  asked in that group.

Sources and Q&A items carry the scope; messages are always scoped by their own
conversation. A question in group A never retrieves group B's scoped knowledge,
but always also sees the global layer. When the same question exists both
globally and as a group variant, the group variant wins in that group.

Scopes are assigned at seed time with `kb seed --scope` and set per group with
`kb group add` (see below).

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

# 2. Push it into D1 through the Worker (global by default; pass --scope with a
#    group chat id to make the entries visible only in that group)
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

Pass `--scope <group-chat-id>` to `kb seed` to tag the imported messages and
their source as belonging to one group instead of the global layer.

---

## Registering the served groups

The bot answers in any group listed in `ALLOWED_TELEGRAM_CHAT_IDS`. Before
seeding group-scoped knowledge (or letting retrieval scope answers), register
each group so it exists in D1 with its title:

```bash
BOT_BASE_URL=https://<worker>.workers.dev uv run kb group add -1001234567890 --title "Prebenjamins"
```

This is idempotent; re-running with a new `--title` refreshes the name. The
Telegram runtime source and the conversation row are created automatically on
the first message from a registered group.

## Reclassifying existing knowledge

Existing rows default to the `global` scope. Knowledge generated from one
group's conversation should be re-scoped to that group:

```bash
npx wrangler d1 execute knowledge-bot --remote --yes \
  --command "UPDATE qa_items SET scope='<group-chat-id>' WHERE id IN (SELECT qa_id FROM qa_versions WHERE origin='auto_generated')"
```

Then `make reindex` so the derived index picks up the new scopes.

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
auditable back to its source. At approval the admin also chooses the **scope**:
🌐 global (all groups) or 👥 a group-only variant of the answer; in its group
the variant outranks the global answer. See [usage.md](usage.md) for the flow.
Knowledge generated from one group's conversation should stay group-scoped;
the reclassification command in the section below fixes existing rows.

---

## Renewing the base from a live source

When the underlying website changes, re-snapshot it and renew the base. Without
`--renew` seeding skips everything it has seen before; with it, entries whose
answer changed get a **new version** on the existing item and become current —
the latest update prevails, and no old version is ever deleted.

```bash
uv run kb snapshot-web --url "https://example.org/faq" --out data/seed/qa.json
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --qa data/seed/qa.json --renew
make reindex
```

A renewal that lands after an approved correction supersedes it (latest wins);
the correction stays in the version history, and the divergence shows up in the
human review report (`kb review`).

---

## Human review report

To see where the base and the corrections diverge and decide what humans should
look at:

```bash
BOT_BASE_URL=https://<worker>.workers.dev uv run kb review --out review.md
```

The report lists, per question: the current answer of each scope, group
variants that differ from the global answer, approved corrections, in-review
entries, and renewals that overwrote recent corrections. It is read-only; acting
on it goes through the normal Telegram correction flow.

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
