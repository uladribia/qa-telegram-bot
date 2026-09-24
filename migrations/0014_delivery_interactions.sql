-- SPDX-License-Identifier: MIT
-- Durable request, delivery, and connector interaction state.

ALTER TABLE bot_answers ADD COLUMN request_id TEXT;
CREATE UNIQUE INDEX idx_bot_answers_request_id
    ON bot_answers(request_id)
    WHERE request_id IS NOT NULL;

CREATE TABLE delivery_receipts (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (object_type, object_id, channel)
);

CREATE TABLE telegram_interactions (
    external_message_id TEXT PRIMARY KEY,
    interaction_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    principal_id TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

UPDATE feedback SET status = 'pending_review' WHERE status = 'pending_admin';
