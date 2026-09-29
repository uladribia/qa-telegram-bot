# Telegram E2E

The primary deployed acceptance gate for real Telegram is the automated
Telethon harness: one command that drives the deployed Worker end to end with
the **existing human admin account** — no second SIM, phone, or Telegram
account, and no browser automation.

```bash
# one time, interactive (login code / 2FA on the terminal)
make telegram-e2e-login

# after a relevant deploy, with explicit authorization
ALLOW_CLOUDFLARE_LIVE_TESTS=1 make test-e2e-telegram
```

## What the harness owns

The split between the offline tier and this harness is deliberate:

- **Offline integration tests own business invariants and adversarial
  authorization** (reviewer permissions, forged approvals, idempotency,
  revert semantics — see `make test-integration`).
- **Telethon owns the real external chain:**
  Telegram MTProto → Bot API webhook → deployed Cloudflare Worker →
  D1 / Vectorize / Workers AI → Telegram outbound → Telethon assertions.

It is a black-box, stateful, sequential scenario — not pytest, not CI, not
part of `make all`/`make test-all`, and never a golden/eval suite. It requires
`ALLOW_CLOUDFLARE_LIVE_TESTS=1`, `.env.e2e` (copy `.env.e2e.example`), two
Telegram groups whose titles begin exactly with `[E2E]`, and the Telethon
session created by `make telegram-e2e-login`. The session file lives under
`.e2e/` and is gitignored credential material.

## The scenario

One run exercises, in order:

1. preflight — resolve the bot and both `[E2E]` groups, bind them into two
   fixed logical spaces **and pin both to `active`** (a run never inherits a
   leftover mode), reset and reseed a sentinel Q&A baseline (global + space A);
2. addressing — mention, `/ask`, reply-to-bot, and bot DM all answer the
   sentinel question;
3. one real abstention for an unknown question;
4. background listener — an unaddressed human question gets no direct reply,
   an explicit human reply is paired and later retrievable;
5. private multi-scope answer — after group traffic, a private question is
   answered from the club's knowledge with its sources;
6. dedup — a run-scoped question, on a subject the corpus says nothing about,
   is seeded into global knowledge only; the projection is waited out in the
   group, and the private question must then be delivered **exactly once** with
   no scope heading. It is deliberately not the sentinel: the harness says the
   sentinel out loud in both groups, so the listener indexes it as message
   evidence and each group's round cites its own, which is two answers by
   design. Nor can it be a paraphrase of the sentinel, which loses the
   similarity floor to it;
7. local reviewer nomination in both groups (the acting account must be the
   deployed admin);
8. reject then re-flag the **same** original answer;
9. edit + group approval; space A answers the corrected value while space B
   keeps the global baseline;
10. global correction from B; B takes the global value while A keeps its local
    override;
11. bot modes — group A is walked through `off`, `silent`, `active` and
    `proactive`, then returned to `active`. What it asserts is whether the bot
    answers, never what it says: by this step the sentinel has been corrected,
    so pinning a token would be asserting the fixture rather than the mode. The
    uninvited probe is the listener step's own question text, because that step
    only passes if the real classifier called it a confident question; the
    sentinel is not a valid probe here, since the real listener reads it as a
    correction and the bot is then right to stay silent;
12. forced daily report delivered to the bot DM (test-only deployment).

Step 6 seeds its own question and reverts it in cleanup, so it leaves the
sentinel fixture alone and does not depend on what earlier runs left in the
groups. Step 11 runs late so a group left in `proactive` cannot answer the
unaddressed traffic the earlier steps assert is silent.

Cleanup always runs: both local reviewers are removed, **both groups are
returned to `active`**, and only the two dedicated sentinel items are reverted.
Old listener messages and feedback history remain in the dedicated test groups;
run tokens (`LOCAL-<run_id>`, `GLOBAL-<run_id>`, …) keep them harmless.

The mode matrix walks a real group, so cleanup restoring `active` is what keeps
a failed or interrupted run from leaving a dedicated group answering
unaddressed questions forever. If a run dies hard enough to skip cleanup, check
the two groups' modes before the next run; the preflight pins them to `active`
either way, but a leftover `off` would silently stop the listener in that group.

The harness never inspects callback payloads (it presses the visible buttons a
human presses), never injects webhook updates, never touches global reviewer
configuration, and never calls the eval/reindex routes.

### What the live run deliberately does not prove

The membership gate on private messages cannot be proven here: the acting
account is the deployed admin, so it is authorized whether or not it has
memberships, and a long-lived production database already records them. What
the live run proves is the consequence — that group traffic is what makes the
private channel answer, and how the answers collapse and label. The gate itself
(a sender with no observed membership gets the one-a-day notice, and is never
stored) is pinned offline in `tests/integration/test_webhook.py`, where the
sender is not the admin. Do not add a destructive internal endpoint to force
this assert into the live run.

## Troubleshooting / reference: the old manual flow

The manual procedure below remains as troubleshooting and reference only; it
is not part of the automated gate and does not run the 57-question manual
checklist (`manual-test-questions.md`).

1. Run `make dev-bootstrap` and `make dev-up`.
2. Create and bind logical spaces A and B.
3. Seed one global Q&A with `Global V1`.
4. Expose the local app through an HTTPS tunnel and register the webhook.
5. Ask the same question in A and B; both answer `Global V1`.
6. Flag A's answer, submit a private proposal, and route it to the local reviewer.
7. Forge a local-reviewer global callback and verify it is denied and acknowledged.
8. Approve the local correction and verify A changes while B remains `Global V1`.
9. Have the global reviewer or admin approve `Global V2`; verify A keeps its local override and B changes.
10. Restart the app and repeat the A/B questions.
11. Trigger `/internal/jobs/daily-report` and inspect privacy-safe logs.
12. Confirm no Cloudflare AI, D1, or Vectorize resource was used.

A reviewer who cannot receive a private message leaves the review pending and is escalated to the admin after the configured timeout.

### What the offline tier already proves

Steps 6 to 9 run without a human, in `make test-integration`, against the
in-process app with in-memory fakes:

- reviewer nomination (local, global, and replacing one), removal, the
  bot-as-reviewer refusal, the non-admin refusal, and the routing ladder from
  the group reviewer to the global reviewer to the admin:
  `tests/integration/test_reviewers_flow.py`.
- reject, re-flag the same answer, and approve the re-opened correction
  locally; a forged global callback and a stranger's confirmation refused; an
  unreachable reviewer escalating to the admin:
  `tests/integration/test_reviewers_flow.py`.
- two groups holding different corrections, retrieved separately, and a global
  correction that leaves a group's local override answering:
  `tests/integration/test_two_group_correction_scenario.py`.
- a failed index after an approval keeping the correction and warning the
  approver, and `POST /internal/revert` restoring the superseded version and its
  projection: `tests/integration/test_reviewers_flow.py`.

What only the automated Telethon gate proves is what the fakes cannot: real
Telegram delivery, the D1 and Vectorize bindings, and the deployed Worker. The
local-only AI run (`make test-e2e-local`) additionally proves the same flows
over SQLite and Ollama.

## Cloudflare acceptance

Only after local acceptance and explicit authorization, deploy test resources, apply migrations, verify the three Vectorize metadata indexes, bind the test groups, and run the smallest direct-answer and correction subset. Do not run a full live eval or full remote reindex as routine setup.
