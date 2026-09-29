# Using the bot

The bot answers addressed questions from global and space-scoped knowledge, cites its sources, and abstains when evidence is insufficient.

## Addressing the bot

The bot answers mentions, replies to its messages, direct messages, and `/ask` questions. What it does with everything else depends on the mode of the conversation.

## Bot modes

Each served group has one mode, and the modes are cumulative:

| Mode | Answers a mention | Answers an unasked question | Stores and indexes what is said |
| --- | --- | --- | --- |
| `off` | no | no | no |
| `silent` | no | no | yes |
| `active` | yes | no | yes |
| `proactive` | yes | yes, when it is a confident question and the answer is grounded | yes |

`proactive` never answers "I don't know": an unaddressed question that comes
back as an abstention or a provider failure produces nothing at all. It also
has its own share of the daily AI budget
(`AI_PROACTIVE_BUDGET_FRACTION`, below the background share), so uninvited
answers are the first thing to stop when the quota runs low.

A mode never disables the control plane. Reviewer commands, replies to
correction prompts, and pending reviews keep working in a group set to `off` or
`silent`.

Private chats have their own mode, `TELEGRAM_DM_BOT_MODE`, with the same four
values. A private message always addresses the bot, so `proactive` behaves like
`active` there and `off` means the bot answers nothing in private at all. A
private message is never turned into knowledge: it is stored when the mode
allows, and never goes through the listener.

Change a group's mode with the same command that registers it. Omitted options
keep what is already registered, so a mode change never renames the group:

```bash
kb group add --chat-id <chat-id> --mode proactive
```

## Who the bot knows in a group

The bot cannot list a group's members without admin rights, and it does not try.
It learns them as it sees them: any message in a served group marks its human
sender as a member, and Telegram's join and leave service messages mark
everyone who arrived or departed. Members who never interact with the bot stay
unknown, and a private message from someone it has never seen in a served group
is refused with a message explaining how to introduce themselves.

## Addressing the bot

Private DMs are restricted to the admin, `ALLOWED_TELEGRAM_USER_IDS`, and the people the bot has observed in a served group. A correction proposal is accepted when it replies to the bot's own correction prompt, even when the reporter is not allowlisted.

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
