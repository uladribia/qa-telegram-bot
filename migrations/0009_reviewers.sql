-- SPDX-License-Identifier: MIT
-- Reviewers: who may confirm corrections, per scope.
--
-- One reviewer per scope ('global' or a group chat id); the admin (env) stays
-- as the fallback reviewer and the only one who can nominate. Reviewer
-- resolutions are recorded as events so the admin can be informed according
-- to ADMIN_REPORT_MODE.

CREATE TABLE reviewers (
    scope TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    nominated_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE reviewer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_id TEXT NOT NULL,
    reviewer_user_id TEXT,
    reviewer_name TEXT,
    group_label TEXT,
    question TEXT,
    action TEXT NOT NULL,
    approval_scope TEXT,
    created_at TEXT NOT NULL,
    reported INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE report_state (
    scope TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);

CREATE INDEX idx_reviewer_events_reported ON reviewer_events(reported);
