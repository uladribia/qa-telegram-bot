# Telegram E2E guide

This is the manual Telegram acceptance path for a deployment or local tunnel.
It is intentionally not part of ordinary `make test` or `make test-integration`.

## Prerequisites

- a Telegram bot token;
- a private test group;
- the bot admin user id;
- a reachable HTTPS Worker URL, or a local tunnel if testing locally;
- `TELEGRAM_WEBHOOK_SECRET` configured identically on the bot and Worker.

Never commit tokens, webhook secrets, or user ids.

## Setup

1. Register the group and its logical space through the authenticated internal
   group-registration operation or the documented CLI equivalent.
2. Configure the Telegram bot token, admin id, allowed user ids, and webhook
   secret in the runtime environment.
3. Deploy or start the runtime.
4. Register the webhook:

   ```bash
   BOT_BASE_URL=https://<worker>.workers.dev uv run kb set-webhook
   ```

5. Verify liveness:

   ```bash
   curl -s https://<worker>.workers.dev/healthz
   ```

## Acceptance flow

1. Mention the bot with a question that has a known Q&A answer.
2. Confirm the answer includes a citation.
3. Press the correction button and submit a proposed correction privately.
4. Have the configured reviewer or admin edit and approve it as local.
5. Ask the original question again and verify the corrected local answer.
6. Ask the same question from another group and verify it keeps its own answer.
7. Nominate a reviewer in the first group and submit another correction.
8. Make the reviewer DM undeliverable in a test environment, wait for the
   configured timeout, and verify the admin receives the escalation message.
9. Run the daily report job and verify the admin receives the deterministic
   sections without raw background question text in the background section.

## Troubleshooting

- `401` webhook response: secret mismatch.
- No answer: verify the group has a channel binding and the message addresses
  the bot or is an allowed direct message.
- Correction prompt not delivered: the user must open a private chat with the
  bot first.
- Reviewer not receiving the task: the configured timeout and activation notice
  are shown in the group/admin messages.
- Daily report missing: verify the Cron Trigger, admin id, and Worker logs.
