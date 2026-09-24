CREATE TABLE message_pair_candidates (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    question_message_id TEXT NOT NULL REFERENCES messages(id),
    answer_message_id TEXT NOT NULL REFERENCES messages(id),
    confidence REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, question_message_id, answer_message_id)
);

CREATE INDEX idx_message_pair_candidates_conversation
    ON message_pair_candidates(conversation_id, created_at);
