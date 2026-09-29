-- SPDX-License-Identifier: MIT
-- Per-conversation bot modes and observed space memberships.
--
-- The bot's behaviour in a group is a property of the channel binding, not of
-- the logical space: two channels bound to the same space can be served with
-- different modes, and a mode never changes what knowledge a space holds.
-- 'active' is the previous behaviour, so every existing binding keeps it.
--
-- Membership is observed, never enumerated. The bot cannot call getChatMember
-- and does not need admin rights, so it records a principal the first time it
-- sees that person in the space, and marks them left when Telegram says so.
-- Members who never interact with the bot stay unknown on purpose.

ALTER TABLE channel_bindings
ADD COLUMN bot_mode TEXT NOT NULL DEFAULT 'active';

CREATE TABLE IF NOT EXISTS space_memberships (
    principal_id TEXT NOT NULL,
    space_id TEXT NOT NULL REFERENCES spaces(id),
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, space_id)
);

CREATE INDEX IF NOT EXISTS idx_space_memberships_principal_status
    ON space_memberships(principal_id, status);
