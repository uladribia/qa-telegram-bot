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

### How retrieval works

When you ask a question, the bot makes exactly one embedding call and then
fuses two rankings:

- **Semantic search** over the derived vector projection. Vectors are
  question-focused: Q&A embeds the canonical question only (the answer stays
  in metadata), a paired message embeds its context question, and a standalone
  factual update embeds its own text.
- **Lexical BM25 search** over an FTS5 projection (`search_fts`) of the same
  question texts. It is a derived index, rebuilt from SQL truth, adding only
  SQL work and no AI quota.

The two ranked lists are combined with Reciprocal Rank Fusion, a group's own
variant suppresses the global answer for the same canonical question, and
authority breaks remaining ties. The answer model (GLM) and correction
workflow are unchanged.

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

The parser stores the base URL and exact source anchor on the Q&A version.
The semantic canonical key is derived separately from the normalized question,
so anchor changes never create a second semantic Q&A item. Citations render the
exact anchored URL from the version provenance.

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

Pass a logical `space_id` to scoped imports. A Telegram chat id is never a knowledge scope; bind the chat to a space first.

---

## Registering the served groups

A Telegram group is served if and only if it has an active `channel_bindings` row. There is no static group allowlist.

Before seeding group-scoped knowledge, register each group so the binding,
logical space, and conversation exist with its title:

```bash
BOT_BASE_URL=https://<worker>.workers.dev uv run kb group add --chat-id -1001234567890 --title "Prebenjamins"
```

The operation is idempotent; re-running with a new `--title` refreshes the name.
The Telegram adapter supplies the Telegram source descriptor, while the shared
space service stores only the opaque channel/external binding. The operation is idempotent; the authenticated internal registration endpoint performs the same binding operation.

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

With `BACKGROUND_LISTENER_ENABLED=true`, every accepted unaddressed message is
stored. Classification controls evidence indexing and reporting, not whether
raw context exists. The bot still never answers these messages.

The classifier embeds each message once and applies a locally trained linear
head (multinomial logistic regression over the embedding, exported by
`scripts/train_classifier.py` to `data/classifier/model.json`), returning a
probability per label (`question`, `knowledge_update`, `correction`,
`chitchat`). A decision is **confident** only when the top probability is at
least `CLASSIFIER_CONFIDENCE` (0.60) and the top1-top2 margin is at least
`CLASSIFIER_MARGIN` (0.15); anything else is operationally ambiguous.
Deterministic acknowledgements (empty, emoji-only, `ok`, `gràcies`,
`perfecte`, ...) skip the embedding call entirely. Confident questions become
pending question candidates; confident updates and corrections are indexed
immediately as factual evidence; chitchat and ambiguous messages are stored
but never indexed or paired.

Answer pairing is fully deterministic and makes no model call. An explicit
Telegram reply from an answer-like message to a confident question pairs
immediately. Otherwise a confident standalone update or correction pairs only
when the conversation has **exactly one** plausible unresolved question inside
the last `PAIRING_QUESTION_WINDOW_MINUTES` (5) and at most
`PAIRING_MAX_PENDING_QUESTIONS` (5) candidates; with zero or several recent
questions the message stays a standalone factual update and nothing is paired
automatically. Sender identity is never used as proof of an answer. Pair
candidates have stable ids, making repeated processing idempotent. A paired
answer is embedded and indexed as message evidence, but it is never promoted
to canonical Q&A.

When the background AI budget is above its configured ceiling, accepted messages
are still stored with deferred classification state and no model call. They do
not run an unbounded automatic catch-up.

---

## Correcting knowledge from Telegram

The normal path, and the only one that produces a human-approved answer:

1. Someone presses `⚠️ Està malament?` on a wrong answer.
2. They propose the correction in a private chat.
3. A local reviewer, global reviewer, or admin receives it according to the review matrix and approves / edits / rejects.

An approval creates a **new version** with the proposer's name and the proposal
date, and supersedes the old one. The version, current pointer, evidence link,
and feedback decision commit in one SQL transaction; a failed write rolls back
all four. The derived vector refresh happens afterward. Nothing is overwritten,
so every answer stays auditable back to its source. At approval the reviewer also chooses the
**scope**: 🌐 global (all groups) or 👥 a group-only variant of the answer; in
its group the variant outranks the global answer. See [usage.md](usage.md) for
the flow, including how the admin nominates reviewers with `/reviewer`.
Knowledge generated from one group's conversation should stay group-scoped; the
reclassification command in the section below fixes existing rows.

Reviewer delivery failures remain pending and escalate to the admin. The deterministic daily report is the only active report mechanism. The admin can roll a correction back with `kb revert` (see [operations.md](operations.md)) — never from Telegram.

---

## Renewing the base from a live source

When the underlying website changes, re-snapshot it and renew the base. Without
`--renew` seeding skips everything it has seen before; with it, entries whose
answer changed get a **new version** on the existing semantic item. No old version
is ever deleted.

```bash
uv run kb snapshot-web --url "https://example.org/faq" --out data/seed/qa.json
BOT_BASE_URL=https://<worker>.workers.dev uv run kb seed --qa data/seed/qa.json --renew
```

A renewal that becomes current is indexed incrementally (~4 neurons each); no
full reindex is needed. A diverged non-current refresh is not indexed until a
human resolves it.

A renewal that lands after an approved correction is stored in version history
but does not replace the human-approved current answer. The seed response reports
`qa_diverged`, and the daily report has a dedicated seed-divergence section. The
correction remains authoritative until a human or an explicit revert changes the
current pointer.

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

- **Never commit raw exports.** `data/raw/*` is gitignored; the bot's
  self-explanation Q&A (`data/seed/bot_self_qa.json`) and the classifier
  artifacts (`data/classifier/model.json`, the 500-case train and test
  splits) are versioned on purpose: the runtime head and the eval splits are
  reproducible build outputs, regenerated by the scripts in `scripts/`.
- Tests use synthetic fixtures only, never a real export.
- Seeding goes through `POST /internal/seed`, not a committed SQL file, so no
  data ever enters git history.

---

## Reindexing

```bash
make reindex                       # or: uv run kb reindex
```

`make reindex` is an explicitly authorized full SQL rebuild. It first calls
`POST /internal/index/cleanup`, which performs no embedding, then the CLI calls
`POST /internal/reindex` repeatedly with a bounded batch of at most 100. Q&A
uses one stable vector id per item (`qa:<qa_item_id>`), so a new version replaces
the old projection instead of leaving a stale searchable version. A resumed run
must pass the last printed cursors and never repeats cleanup.

**It is rarely the right tool.** Incremental paths cover routine changes: the
seed indexes the Q&A it creates or renews (~4 neurons each), and an approved
correction or a revert refreshes the same stable Q&A item vector. A full
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
