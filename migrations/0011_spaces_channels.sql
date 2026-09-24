-- SPDX-License-Identifier: MIT
-- Logical spaces and channel bindings.
--
-- External channel identifiers are binding values, never knowledge scopes.

CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_bindings (
    channel TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    space_id TEXT NOT NULL REFERENCES spaces(id),
    title TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, external_conversation_id),
    UNIQUE (conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_bindings_space
    ON channel_bindings(space_id);

ALTER TABLE conversations ADD COLUMN space_id TEXT REFERENCES spaces(id);
ALTER TABLE bot_answers ADD COLUMN space_id TEXT;
ALTER TABLE feedback ADD COLUMN origin_space_id TEXT;
ALTER TABLE feedback ADD COLUMN reporter_principal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_conversations_space
    ON conversations(space_id);
