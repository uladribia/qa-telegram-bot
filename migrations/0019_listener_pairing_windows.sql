CREATE TABLE listener_pairing_windows (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    processed_at TEXT,
    status TEXT NOT NULL
);

CREATE INDEX idx_listener_pairing_windows_due
    ON listener_pairing_windows(status, last_message_at);
