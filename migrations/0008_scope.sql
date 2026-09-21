-- SPDX-License-Identifier: MIT
-- Scope: global knowledge vs per-group knowledge.
--
-- Sources get an explicit scope column. Q&A items are rebuilt so that
-- canonical_key is unique per scope instead of globally: the same canonical
-- question may exist once globally and once per group (a group-specific
-- variant of a global answer).
--
-- D1 wraps a migration file in a single transaction and ignores PRAGMA
-- changes, so the parent table cannot simply be dropped while qa_versions
-- references it. The chain is rebuilt child-first instead: qa_evidence, then
-- qa_versions, then qa_items, each with its data copied around the drop.

ALTER TABLE sources
    ADD COLUMN scope TEXT NOT NULL DEFAULT 'global';

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

CREATE TABLE qa_versions_new (
    id TEXT PRIMARY KEY,
    qa_id TEXT NOT NULL,
    answer TEXT NOT NULL,
    authority INTEGER NOT NULL,
    confidence REAL,
    origin TEXT NOT NULL,
    created_by TEXT,
    supersedes_version_id TEXT,
    created_at TEXT NOT NULL,
    source_url TEXT,
    author TEXT
);

INSERT INTO qa_versions_new
    (id, qa_id, answer, authority, confidence, origin, created_by,
     supersedes_version_id, created_at, source_url, author)
SELECT id, qa_id, answer, authority, confidence, origin, created_by,
       supersedes_version_id, created_at, source_url, author
FROM qa_versions;

CREATE TABLE qa_evidence_new (
    qa_version_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (qa_version_id, evidence_type, evidence_id)
);

INSERT INTO qa_evidence_new
    (qa_version_id, evidence_type, evidence_id)
SELECT qa_version_id, evidence_type, evidence_id
FROM qa_evidence;

DROP TABLE qa_evidence;
DROP TABLE qa_versions;
DROP TABLE qa_items;

ALTER TABLE qa_items_new RENAME TO qa_items;

CREATE TABLE qa_versions (
    id TEXT PRIMARY KEY,
    qa_id TEXT NOT NULL REFERENCES qa_items(id),
    answer TEXT NOT NULL,
    authority INTEGER NOT NULL,
    confidence REAL,
    origin TEXT NOT NULL,
    created_by TEXT,
    supersedes_version_id TEXT,
    created_at TEXT NOT NULL,
    source_url TEXT,
    author TEXT
);

INSERT INTO qa_versions
    (id, qa_id, answer, authority, confidence, origin, created_by,
     supersedes_version_id, created_at, source_url, author)
SELECT id, qa_id, answer, authority, confidence, origin, created_by,
       supersedes_version_id, created_at, source_url, author
FROM qa_versions_new;

DROP TABLE qa_versions_new;

CREATE TABLE qa_evidence (
    qa_version_id TEXT NOT NULL REFERENCES qa_versions(id),
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (qa_version_id, evidence_type, evidence_id)
);

INSERT INTO qa_evidence
    (qa_version_id, evidence_type, evidence_id)
SELECT qa_version_id, evidence_type, evidence_id
FROM qa_evidence_new;

DROP TABLE qa_evidence_new;

CREATE INDEX IF NOT EXISTS idx_qa_items_status ON qa_items(status);
CREATE INDEX IF NOT EXISTS idx_qa_items_scope ON qa_items(scope);
CREATE INDEX IF NOT EXISTS idx_qa_versions_qa ON qa_versions(qa_id);
CREATE INDEX IF NOT EXISTS idx_sources_scope ON sources(scope);
