-- SPDX-License-Identifier: MIT
-- Background classification and evidence-index state.

ALTER TABLE messages ADD COLUMN sender_authority INTEGER;
ALTER TABLE messages ADD COLUMN classification_status TEXT;
ALTER TABLE messages ADD COLUMN intent_scores_json TEXT;
ALTER TABLE messages ADD COLUMN index_status TEXT NOT NULL DEFAULT 'not_indexed';
ALTER TABLE messages ADD COLUMN indexed_at TEXT;
