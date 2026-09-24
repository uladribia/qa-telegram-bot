# Using the bot

The bot answers addressed questions from global and space-scoped knowledge, cites its sources, and abstains when evidence is insufficient.

## Addressing the bot

The bot answers mentions, replies to its messages, direct messages, and `/ask` questions. Unaddressed group traffic is not answered. With `BACKGROUND_LISTENER_ENABLED=true`, accepted background messages are stored and eligible evidence is indexed without answering the group.

Private DMs are restricted to the admin and `ALLOWED_TELEGRAM_USER_IDS`. A correction proposal is accepted when it replies to the bot's own correction prompt, even when the reporter is not allowlisted.

A Telegram group is served only when its chat is bound to a logical space. There is no static group allowlist.

## Answer outcomes

- **Direct**: a strong current Q&A match is returned verbatim with its citation.
- **Synthesis**: weaker evidence is sent through one grounded generation call.
- **Abstention**: no reliable evidence exists.
- **Unavailable**: the model is temporarily unavailable; the inbound question remains durable.

## Corrections

1. Press `⚠️ Està malament?` on an answer.
2. Reply to the bot's private proposal prompt with the correction.
3. The proposal is routed privately to the space reviewer, global reviewer, or admin.
4. The reviewer edits, rejects, approves locally, or approves globally.
5. A future question in the origin space sees a local override; other spaces keep the global answer.

A local reviewer cannot approve global knowledge. A global reviewer or admin can choose either scope. A wrong principal cannot consume another principal's reply prompt. Resolved feedback cannot be approved or rejected again.

## Reviewer roles

- **Anyone in a group** can flag an answer and propose a correction.
- **Local reviewer** can review, edit, reject, and approve local scope for their own space only.
- **Global reviewer** can review and approve local or global scope.
- **Admin** has the same approval authority and is the only role that can nominate or remove reviewers and revert knowledge.

The admin is notified when a reviewer cannot be reached. The review remains pending until the configured escalation timeout.

## Daily report

The deterministic daily report is sent by the scheduled Worker handler. It includes addressed outcomes, background questions, indexed evidence, corrections, seed divergence, projection repair counts, and estimated AI usage. Manual preview and delivery use `POST /internal/jobs/daily-report`; `dry_run=true` does not send or update report state.

## Media and limits

Attachments are metadata-only in v1; media is never processed. Workers AI quota exhaustion produces a temporary unavailable answer. See [operations.md](operations.md).
