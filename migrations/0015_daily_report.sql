-- SPDX-License-Identifier: MIT
-- Last successful scheduled daily report.

CREATE TABLE daily_report_state (
    key TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
