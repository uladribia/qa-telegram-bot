-- SPDX-License-Identifier: MIT
-- Final hardening: durable answer rendering, principals, and projection state.

ALTER TABLE bot_answers ADD COLUMN rendered_text TEXT;
ALTER TABLE bot_answers ADD COLUMN source_details_json TEXT NOT NULL DEFAULT '[]';
UPDATE bot_answers SET rendered_text = answer WHERE rendered_text IS NULL;

UPDATE feedback
SET reporter_principal_id = 'telegram:' || reporter_chat_id
WHERE reporter_principal_id IS NULL AND reporter_chat_id IS NOT NULL AND reporter_chat_id <> '';
UPDATE feedback
SET origin_space_id = (SELECT bot_answers.space_id FROM bot_answers WHERE bot_answers.id = feedback.bot_answer_id)
WHERE origin_space_id IS NULL;

CREATE TABLE reviewers_final (
    scope TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    name TEXT NOT NULL,
    nominated_by_principal_id TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO reviewers_final(scope, principal_id, name, nominated_by_principal_id, created_at)
SELECT scope,
       CASE WHEN instr(user_id, ':') > 0 THEN user_id ELSE 'telegram:' || user_id END,
       name,
       CASE WHEN nominated_by IS NULL THEN NULL WHEN instr(nominated_by, ':') > 0 THEN nominated_by ELSE 'telegram:' || nominated_by END,
       created_at
FROM reviewers;
DROP TABLE reviewers;
ALTER TABLE reviewers_final RENAME TO reviewers;

CREATE TABLE reviewer_events_final (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id TEXT NOT NULL,
    reviewer_principal_id TEXT,
    reviewer_name TEXT,
    group_label TEXT,
    question TEXT,
    action TEXT NOT NULL,
    approval_scope TEXT,
    created_at TEXT NOT NULL,
    reported INTEGER NOT NULL DEFAULT 0
);
INSERT INTO reviewer_events_final(id, feedback_id, reviewer_principal_id, reviewer_name, group_label, question, action, approval_scope, created_at, reported)
SELECT id, feedback_id,
       CASE WHEN reviewer_user_id IS NULL THEN NULL WHEN instr(reviewer_user_id, ':') > 0 THEN reviewer_user_id ELSE 'telegram:' || reviewer_user_id END,
       reviewer_name, group_label, question, action, approval_scope, created_at, reported
FROM reviewer_events;
DROP TABLE reviewer_events;
ALTER TABLE reviewer_events_final RENAME TO reviewer_events;
CREATE INDEX idx_reviewer_events_reported ON reviewer_events(reported);

ALTER TABLE search_projection ADD COLUMN version_id TEXT;
ALTER TABLE search_projection ADD COLUMN state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE search_projection ADD COLUMN last_error TEXT;
CREATE INDEX idx_search_projection_state ON search_projection(state);
