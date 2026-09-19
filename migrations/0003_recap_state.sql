-- SPDX-License-Identifier: MIT
-- State required by the opportunistic recap trigger.

CREATE TABLE IF NOT EXISTS recap_state (
    conversation_id TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
