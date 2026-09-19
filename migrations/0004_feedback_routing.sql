-- SPDX-License-Identifier: MIT
-- Correction routing: the flow happens in private chats, so feedback needs the
-- reporter's chat and the prompt message ids used to correlate replies.

ALTER TABLE feedback ADD COLUMN reporter_chat_id TEXT;
ALTER TABLE feedback ADD COLUMN proposal_prompt_message_id TEXT;
ALTER TABLE feedback ADD COLUMN edit_prompt_message_id TEXT;
