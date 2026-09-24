# Telegram E2E

Real Telegram acceptance remains a manual external gate. It is not run by offline CI or by the implementation pass.

Use the complete question checklist in [`manual-test-questions.md`](manual-test-questions.md). It covers every active and `in_review` entry in the club Q&A seed, bot self-knowledge, retrieval paraphrases, and negative cases.

Use a test bot and two test groups. The canonical local webhook route is `/telegram/webhook`.

## Local manual flow

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

## Cloudflare acceptance

Only after local acceptance and explicit authorization, deploy test resources, apply migrations, verify the three Vectorize metadata indexes, bind the test groups, and run the smallest direct-answer and correction subset. Do not run a full live eval or full remote reindex as routine setup.
