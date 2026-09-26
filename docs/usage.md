# Using the bot

The bot answers addressed questions from global and space-scoped knowledge, cites its sources, and abstains when evidence is insufficient.

## Addressing the bot

The bot answers mentions, replies to its messages, direct messages, and `/ask` questions. Unaddressed group traffic is not answered. With `BACKGROUND_LISTENER_ENABLED=true`, accepted background messages are stored and eligible evidence is indexed without answering the group.

Private DMs are restricted to the admin and `ALLOWED_TELEGRAM_USER_IDS`. A correction proposal is accepted when it replies to the bot's own correction prompt, even when the reporter is not allowlisted.

A Telegram group is served only when its chat is bound to a logical space. There is no static group allowlist.

## Answer outcomes

- **Synthesis**: the retrieved Q&A items that clear the similarity floor are sent through one grounded generation call, and the answer cites them. This is the only answering path.
- **Abstention**: no retrieved item clears the floor, or the model finds the evidence insufficient. The model is allowed to decline, so an unknown question is never answered with an unrelated document.
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

The deterministic daily report covers a completed day: addressed outcomes, background questions, indexed evidence, corrections, seed divergence, projection repair counts, and estimated AI usage.

**It is delivered by an external scheduler, not by the Worker.** The `0 19 * * *` Cron Trigger is registered but cannot run on the Workers Free plan, which allots a cron invocation 10 ms of CPU against a 3.4 s Python interpreter start-up, so `Default.scheduled` never reaches its first statement. The route itself is fine and sends whenever it is called. Point a scheduler (GitHub Actions or equivalent) at `POST /internal/jobs/daily-report` with the admin key. `dry_run=true` renders the same text without sending or updating report state, and the job skips when less than 24 h has passed since the last successful send. `daily_report_state` is the ground truth: an empty table means the scheduler has never delivered one. See [operations.md](operations.md#daily-report).

## Media and limits

Attachments are metadata-only in v1; media is never processed. Every answered question is grounded in the retrieved Q&A and group evidence; when the evidence does not support an answer, or the quota is exhausted, the bot says so instead of guessing. On a given question the model may decline an answer that a previous identical question received, so repeated asks of the same question are not guaranteed to agree. See [operations.md](operations.md).
