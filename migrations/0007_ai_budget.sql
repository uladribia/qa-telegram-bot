-- SPDX-License-Identifier: MIT
-- Workers AI spend, estimated per UTC day.
--
-- The free plan allows 10k neurons/day. Nothing in the Worker can read the real
-- account usage, so calls are metered with a char-based estimate. The meter is
-- deliberately conservative and only ever *refuses expensive admin paths*
-- (evals, reindex): real user questions keep working and fail over to the
-- temporary-unavailable reply instead.

CREATE TABLE IF NOT EXISTS ai_budget (
    day TEXT PRIMARY KEY,
    neurons REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
