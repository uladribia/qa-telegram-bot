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
3. DM to Quim Quim writes the correction → "Gràcies. Ho he enviat a revisió."
4. DM to admin the bot forwards the proposal with  [✅ Aprovar] [✏️ Editar] [❌ Rebutjar]
5. DM to admin admin presses ✅
6. Group      a future equivalent question gets the corrected answer
7. DM to Quim "Gràcies per la correcció" (private thank-you)
```

Who may do what:

- **Anyone in the group** can flag an answer and propose a correction.
- **Only the admin** (`ADMIN_TELEGRAM_USER_ID`) can approve, edit or reject. This
  is enforced server-side: a confirmation from anyone else is ignored, not just
  hidden.
- Reporting needs no allowlisting: the proposal is a reply to a prompt the bot
  sent, so it is accepted regardless of `ALLOWED_TELEGRAM_USER_IDS`.
- Approving does **not** overwrite anything. It adds a new version; the old one is
  kept, and the web Q&A is never modified.

`✏️ Editar` shows the current proposal and asks for the corrected text; the edited
version comes back to the admin with the same three buttons.

---

## The periodic recap

To stop unanswered questions being lost, the bot can post a periodic summary of
the window's questions, marking the ones it could not answer as pending.

Configured with `RECAP_ENABLED`, `RECAP_INTERVAL_HOURS`, `RECAP_LANGUAGE`.

It is checked **opportunistically** on inbound updates, because a Cloudflare
Python Worker only exposes a `fetch` handler — there is no cron. If nothing at all
happens in the group, no recap is due; an external scheduler can poke
`POST /internal/recap` with the internal key.

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
