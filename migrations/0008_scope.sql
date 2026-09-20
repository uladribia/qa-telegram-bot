-- SPDX-License-Identifier: MIT
-- Scope: global knowledge vs per-group knowledge.
--
-- Sources get an explicit scope column. Q&A items are rebuilt so that
-- canonical_key is unique per scope instead of globally: the same canonical
-- question may exist once globally and once per group (a group-specific
-- variant of a global answer).

ALTER TABLE sources
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'global';

PRAGMA defer_foreign_keys = ON;

CREATE TABLE qa_items_new (
    id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    canonical_question TEXT NOT NULL,
    status TEXT NOT NULL,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    UNIQUE (canonical_key, scope)
);

INSERT INTO qa_items_new
    (id, canonical_key, canonical_question, status, current_version_id,
     created_at, updated_at, scope)
SELECT id, canonical_key, canonical_question, status, current_version_id,
       created_at, updated_at, 'global'
FROM qa_items;

DROP TABLE qa_items;

ALTER TABLE qa_items_new RENAME TO qa_items;

CREATE INDEX IF NOT EXISTS idx_qa_items_scope ON qa_items(scope);
CREATE INDEX IF NOT EXISTS idx_sources_scope ON sources(scope);
