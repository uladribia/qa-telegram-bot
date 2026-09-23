-- SPDX-License-Identifier: MIT
-- Listener intent: classification labels and matched questions on messages.
--
-- The background listener classifies unaddressed group traffic with the
-- embedding-prototype classifier (plan §12). Only clear-cut chitchat is
-- discarded; everything else is ingested with its winning label and score.
-- A reply that answers a parent question stores the parent's question text so
-- reindex can embed the pair together.

ALTER TABLE messages ADD COLUMN intent_label TEXT;
ALTER TABLE messages ADD COLUMN intent_score REAL;
ALTER TABLE messages ADD COLUMN context_question TEXT;

CREATE INDEX idx_messages_intent_created
    ON messages(intent_label, created_at);
