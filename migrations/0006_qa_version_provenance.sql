-- SPDX-License-Identifier: MIT
-- Citations must name the real origin of a Q&A version.
--
-- Until now every version was cited with the web-seed URL, so an approved
-- correction wrongly claimed the website as its source. A version now carries
-- its own origin: `source_url` for a web snapshot entry, `author` for a
-- human correction (the proposer's display name).

ALTER TABLE qa_versions ADD COLUMN source_url TEXT;
ALTER TABLE qa_versions ADD COLUMN author TEXT;

-- The correction flow cites the proposal's author and date, so feedback keeps
-- the proposer's name and the moment the proposal was made.
ALTER TABLE feedback ADD COLUMN reporter_name TEXT;
ALTER TABLE feedback ADD COLUMN proposed_at TEXT;
