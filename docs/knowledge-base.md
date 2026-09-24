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

Source identity and base authority are declared by the connector that imports
or receives the material. The shared application does not contain a registry of
channel names: a new connector can provide its own source kind and source
instance without adding a branch to ingestion or space management.

---

## Adding knowledge from a web page

Parse the page into a JSON file, then push it to the deployed Worker.

```bash
# 1. Snapshot the Q&A page into data/seed/qa.json
uv run kb snapshot-web --url "https://example.org/faq" --out data/seed/qa.json

# 2. Push it into D1 through the Worker (global by default; pass --scope with a
#    group chat id to make the entries visible only in that group).
#    New and renewed entries are indexed incrementally as part of the seed:
#    ~4 neurons each, no full rebuild needed.
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --qa data/seed/qa.json
```

The parser is generic, not tied to one site. It is configured for the headings and
answer shapes the page uses; if a different page yields too few entries it refuses
to write rather than seeding nonsense (the floor is 30 entries).

**Then**: nothing — the seed indexes what it created. A full `make reindex` is
not needed for routine additions, and is a user-authorized operation reserved
for substantial changes (see [operations.md](operations.md)).

The parser stores the **exact anchored URL** for each entry, which is what
citations show.

---

## Adding knowledge from a WhatsApp export

```bash
uv run kb import-whatsapp data/raw/whatsapp.txt --out data/seed/whatsapp.jsonl
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --messages data/seed/whatsapp.jsonl
```

Imported messages are **not** auto-indexed: an export is typically thousands of
messages, so indexing it is a full rebuild — a substantial change that needs the
user's explicit authorization and the budget ritual in
[operations.md](operations.md).

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

A Telegram group must be both listed in `ALLOWED_TELEGRAM_CHAT_IDS` and bound to
a logical space. The allow-list is connector configuration; the durable channel
binding is the application-level space relationship. An allow-listed group with
no binding is ignored.

Before seeding group-scoped knowledge, register each group so the binding,
logical space, and conversation exist with its title:

```bash
BOT_BASE_URL=https://<worker>.workers.dev uv run kb group add --chat-id -1001234567890 --title "Prebenjamins"
```

The operation is idempotent; re-running with a new `--title` refreshes the name.
The Telegram adapter supplies the Telegram source descriptor, while the shared
space service stores only the opaque channel/external binding. The v2 canonical
CLI will expose this as `kb channel bind telegram`; until that CLI phase lands,
the authenticated internal registration endpoint performs the same operation.

## Reclassifying existing knowledge

Existing rows default to the `global` scope. Knowledge generated from one
group's conversation should be re-scoped to that group:

```bash
npx wrangler d1 execute knowledge-bot --remote --yes \
  --command "UPDATE qa_items SET scope='<group-chat-id>' WHERE id IN (SELECT qa_id FROM qa_versions WHERE origin='auto_generated')"
```

Then `make reindex` so the derived index picks up the new scopes (a user-
authorized full rebuild: scope changes alter the metadata of every record).

---

## Adding knowledge by talking to the bot

With `BACKGROUND_LISTENER_ENABLED=true`, unaddressed group messages are
classified and stored as low-authority knowledge. The bot still does not
answer them. This is how the base grows from real conversation.

The classifier (plan §12) embeds each message once, in the same batch as a
set of Catalan prototype phrases, and scores it by best cosine similarity
per label (`question`, `knowledge_update`, `correction`, `chitchat`). The
scores are similarities, never probabilities. The policy is conservative:
only a message scoring strongly as chitchat (`CLASSIFIER_CHITCHAT_DISCARD`,
default 0.80) while no other label clears the keep signal
(`CLASSIFIER_KEEP_SIGNAL`, default 0.45) is discarded; everything else is
kept as context, with its winning label stored on the message row.

A reply that looks like an answer (`CLASSIFIER_ANSWER_MATCH`, default 0.55)
to a stored message that looks like a question
(`CLASSIFIER_QUESTION_MATCH`, default 0.60) is matched into a
question-answer pair: the parent question is stored on the reply, and
reindex embeds the pair together so it retrieves as one unit instead of an
orphaned answer.

Keep the listener **off** unless you want that: it stores everything said in
the group that is not clear-cut chitchat.

---

## Correcting knowledge from Telegram

The normal path, and the only one that produces a human-approved answer:

1. Someone presses `⚠️ Està malament?` on a wrong answer.
2. They propose the correction in a private chat.
3. The admin gets it privately and approves / edits / rejects.

An approval creates a **new version** with the proposer's name and the proposal
date, and supersedes the old one. Nothing is overwritten, so every answer stays
auditable back to its source. At approval the reviewer also chooses the
**scope**: 🌐 global (all groups) or 👥 a group-only variant of the answer; in
its group the variant outranks the global answer. See [usage.md](usage.md) for
the flow, including how the admin nominates reviewers with `/reviewer`.
Knowledge generated from one group's conversation should stay group-scoped; the
reclassification command in the section below fixes existing rows.

The admin is informed of every reviewer resolution (`ADMIN_REPORT_MODE`:
`always`, `batch` or `off`) and can roll a correction back with `kb revert`
(see [operations.md](operations.md)) — never from Telegram.

---

## Renewing the base from a live source

When the underlying website changes, re-snapshot it and renew the base. Without
`--renew` seeding skips everything it has seen before; with it, entries whose
answer changed get a **new version** on the existing item and become current —
the latest update prevails, and no old version is ever deleted.

```bash
uv run kb snapshot-web --url "https://example.org/faq" --out data/seed/qa.json
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --qa data/seed/qa.json --renew
```

Renewed entries are indexed incrementally by the seed itself (~4 neurons each);
no full reindex is needed.

A renewal that lands after an approved correction supersedes it (latest wins);
the correction stays in the version history, and the divergence shows up in the
human review report (`kb review`).

---

## The bot explaining itself

The bot answers questions about itself ("qui ets?", "com funciones?", "com
corregeixo una resposta?") from a static, versioned Q&A file:
`data/seed/bot_self_qa.json`. It is generated from the documentation at
each release tag — no runtime LLM generation — and seeded as **global**
knowledge, so every deployment serves the same self-explanation:

```bash
make seed-self-qa      # = kb seed --qa data/seed/bot_self_qa.json
```

It is idempotent; after editing an answer's text, seed with `--renew`. When
the bot's behaviour, flows, or limits change, update these entries in the same
branch (see AGENTS.md §10.1) and never expose internals (tables, secrets,
config keys) in them.

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

**It is rarely the right tool.** Incremental paths cover routine changes: the
seed indexes the Q&A it creates or renews (~4 neurons each), and an approved
correction or a revert reindexes its single version automatically. A full
rebuild is a user-authorized operation for substantial changes only — see the
authorization ritual in [operations.md](operations.md).

Two things to expect:

- It **costs AI quota**. On the free plan 10k neurons/day, a full rebuild is a
  large bite — don't run it casually. See [operations.md](operations.md).
- Vectorize is **eventually consistent**. Right after a reindex, queries can read
  stale metadata, and fresh upserts can be unqueryable for a while: usually
  about a minute, but measured up to ~25 minutes under free-tier load
  (2026-09-22). If results look impossible, wait and retry before believing
  them.
