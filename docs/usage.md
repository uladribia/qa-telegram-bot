# Using the bot

What the bot does, what you type, and what happens next.

---

## What it is

A knowledge bot for a Telegram group. It answers questions from a curated
knowledge base, **always with a source**, and it says "I don't know" instead of
inventing. Anyone in the group can flag a wrong answer and propose a fix; only
the admin can approve it.

It is **not** a general assistant. It only answers from what is in its knowledge
base.

---

## When it answers

The bot is silent unless it is addressed. A message is answered when it is any of:

| How | Example |
|---|---|
| A mention | `@bhc_minis_knowledge_test_bot quan entrenen?` |
| A reply to one of its messages | reply with your question |
| A direct message | just type the question |
| The `/ask` command | `/ask quan entrenen?` |

Anything else is ignored. Unaddressed group chatter is not answered. (There is an
off-by-default switch, `BACKGROUND_LISTENER_ENABLED`, that stores group traffic as
knowledge without answering it. See [knowledge-base.md](knowledge-base.md).)

### Direct messages

A bot username is public, so direct messages are **restricted**: only the admin and
the users listed in `ALLOWED_TELEGRAM_USER_IDS` get an answer there. Anyone else is
ignored silently.

The one exception is the correction flow: if the bot has just asked someone for a
correction, their reply is accepted even if they are not on the list — they never
have to be allowlisted to propose a fix.

---

## Typical flow: asking a question

```text
Quim  › @bot quan s'ha de demanar l'equipament?
Bot   › Es demana a l'inici de temporada, normalment abans de començar
        els entrenaments.

        Fonts:
        • Q&A · Com i quan s'ha de demanar l'equipament? · https://…#qa-equipament-com-demanar · 12/09/2026
        • Grup · Nom Cognom · 09/04/2026 07:32

        [⚠️ Està malament?]
```

Every answer carries a button. The bot decides one of three ways:

| Outcome | When | What you see |
|---|---|---|
| **Direct** | A stored Q&A matches closely | that answer verbatim, cited |
| **Synthesised** | Several weaker sources together answer it | a written answer, cited |
| **Abstention** | Nothing in the knowledge base covers it | "No tinc prou informació fiable per respondre-ho." |

Abstention is a feature, not a failure. A wrong confident answer is worse than no
answer.

### How to read the citations

| Source | Cited as |
|---|---|
| Website Q&A | the **exact anchored URL** + date |
| Group message | the **author's name** + date and time |
| An approved correction | the **proposer's name** + date of the proposal |

---

## Typical flow: correcting a wrong answer

The whole correction happens in **private chats**. The group never sees the
discussion.

```text
1. Group      Quim presses  [⚠️ Està malament?]  on the wrong answer
2. DM to Quim the bot asks: "Què corregiries? Escriu la resposta correcta…"
   and repeats the answer being corrected, for context.
3. DM to Quim Quim writes the correction → "Gràcies. Ho he enviat a revisió."
4. DM to the group's reviewer (or the global reviewer, or the admin)
              the bot forwards the proposal with [🌐 Aprovar global] [👥 Aprovar grup]
              [✏️ Editar] [❌ Rebutjar]
5. DM         the reviewer presses 🌐 or 👥
6. Group      a future equivalent question gets the corrected answer
              (it can take up to a minute for the index to catch up)
7. DM to Quim "Gràcies per la correcció" (private thank-you)
```

Telegram only lets a bot DM someone who has **opened a private chat with it at
least once** (pressed Start on its profile). Anyone who wants to flag answers or
serve as a reviewer must do that first; otherwise the bot cannot reach them.
When it happens anyway, the bot says so instead of failing silently: the button
press shows a popup asking to open the chat first, the group gets a message
with a direct `t.me` link to the bot, and if a review cannot be delivered to
its reviewer, the admin receives the full review — buttons included — and may
resolve it themselves.

Who may do what:

- **Anyone in the group** can flag an answer and propose a correction.
- **Corrections are confirmed by reviewers**: the reviewer nominated for the
  group the answer came from, or the global reviewer when that group has none,
  or the admin when nobody is nominated. Anyone nominated may approve, edit or
  reject — and choose 🌐 or 👥 at approval time. This is enforced server-side:
  a confirmation from anyone else is ignored, not just hidden.
- The admin is the fallback reviewer and the only one who can nominate or
  remove reviewers (see below) or roll a correction back (CLI only, see
  [operations.md](operations.md)).
- Reporting needs no allowlisting: the proposal is a reply to a prompt the bot
  sent, so it is accepted regardless of `ALLOWED_TELEGRAM_USER_IDS`.
- Approving does **not** overwrite anything. It adds a new version; the old one is
  kept, and the web Q&A is never modified.
- The reviewer chooses the **scope** of the corrected answer at approval time:
  🌐 makes it the global answer (every group sees it), 👥 makes it a
  group-only variant (only the group the corrected answer came from sees it).
  In that group the variant outranks the global answer; other groups keep
  seeing the global one.

`✏️ Editar` shows the current proposal and asks for the corrected text; the edited
version comes back to the reviewer's DM with the same three buttons.

### Nominating reviewers

Only the admin can nominate, and only from Telegram, by **replying to a message
of the person** (their Telegram user id is what gets stored; usernames are not
used):

| Action | Where | Command |
|---|---|---|
| Nominate the group's reviewer | in the group | reply to their message with `/reviewer` |
| Nominate the global reviewer | any group | reply to their message with `/reviewer global` |
| List current reviewers | any chat | `/reviewer` (no reply) |
| Remove the group's reviewer | in the group | `/reviewer off` |
| Remove the global reviewer | any chat | `/reviewer off global` |

Nominating again replaces the previous reviewer of that scope; there is no
history — it is an operational role, not knowledge. The bot itself can never be
nominated: replying `/reviewer` to one of its messages is refused.

### The admin report on reviewer corrections

The admin cannot act on reviewer decisions from Telegram, but is informed of
every one of them, configured with `ADMIN_REPORT_MODE`:

- `always` (default): one private report per resolution — who, which group,
  which question, what they did (approved global / approved group / edited /
  rejected), and when. Every report ends with the day's estimated AI usage
  (neurons spent against the daily limit, and the estimated call count) so the
  admin sees how close the quota is without checking Cloudflare.
- `batch`: one consolidated report every `ADMIN_REPORT_INTERVAL_MIN` minutes.
  Like the recap, the check is opportunistic on inbound events; an external
  scheduler can also poke `POST /internal/report`. The usage line is included
  here too.
- `off`: no reports. Events are still recorded in D1.

The report is read-only. Rolling a correction back is a CLI operation only
(`kb revert`, see [operations.md](operations.md)).

---

## The periodic recap

To stop unanswered questions being lost, the bot sends the **admin** a daily
summary of every group's questions, each tagged with the group it came from and
the ones it could not answer marked as pending. **Groups never receive
summaries** — posting them in a group leaked other groups' questions and
interrupted chats where the bot had answered nothing.

Configured with `RECAP_ENABLED`, `RECAP_INTERVAL_HOURS`, `RECAP_LANGUAGE`.

It is checked **opportunistically** on inbound updates, because a Cloudflare
Python Worker only exposes a `fetch` handler — there is no cron. If nothing at all
happens anywhere, no recap is due; an external scheduler can poke
`POST /internal/recap` with the internal key to force the check.

---

## Media

v1 records photos and other attachments as **metadata only**. It is registered,
never downloaded and never processed. Sending a photo will not get an answer.

---

## Limits worth knowing

- Answers come **only** from the knowledge base. An empty base means abstentions.
- The daily Workers AI budget can run out, after which questions get
  *"Ara mateix no puc consultar la informació"* until 00:00 UTC. See
  [operations.md](operations.md).
- There is no user authentication: access control is the allowed chat id.
