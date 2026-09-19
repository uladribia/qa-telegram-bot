-- SPDX-License-Identifier: MIT
-- Indexes for the query paths used by the core (spec §9, §48, §49).

CREATE INDEX IF NOT EXISTS idx_conversations_source
    ON conversations(source_id);

CREATE INDEX IF NOT EXISTS idx_messages_source
    ON messages(source_id);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, sent_at);

CREATE INDEX IF NOT EXISTS idx_attachments_message
    ON attachments(message_id);

CREATE INDEX IF NOT EXISTS idx_qa_items_status
    ON qa_items(status);

CREATE INDEX IF NOT EXISTS idx_qa_versions_qa
    ON qa_versions(qa_id);

CREATE INDEX IF NOT EXISTS idx_bot_answers_conversation
    ON bot_answers(conversation_id);

CREATE INDEX IF NOT EXISTS idx_feedback_status
    ON feedback(status);

CREATE INDEX IF NOT EXISTS idx_feedback_bot_answer
    ON feedback(bot_answer_id);
