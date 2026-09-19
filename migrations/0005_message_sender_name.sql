-- SPDX-License-Identifier: MIT
-- Group citations show the author's display name (decided: names are stored for
-- citation purposes only, never logged).

ALTER TABLE messages ADD COLUMN sender_name TEXT;
