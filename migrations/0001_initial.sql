-- SPDX-License-Identifier: MIT
-- Initial schema for the knowledge bot (spec §9).

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    external_ref TEXT,
    title TEXT,
    canonical_url TEXT,
    authority INTEGER NOT NULL,
    is_mutable INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    external_id TEXT,
    sender_hash TEXT,
    sender_is_admin INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL,
    text TEXT,
    content_type TEXT NOT NULL,
    reply_to_message_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id),
    kind TEXT NOT NULL,
    external_file_id TEXT,
    external_unique_id TEXT,
    file_name TEXT,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    size_bytes INTEGER,
    processing_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_items (
    id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    canonical_question TEXT NOT NULL,
    status TEXT NOT NULL,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_versions (
    id TEXT PRIMARY KEY,
    qa_id TEXT NOT NULL REFERENCES qa_items(id),
    answer TEXT NOT NULL,
    authority INTEGER NOT NULL,
    confidence REAL,
    origin TEXT NOT NULL,
    created_by TEXT,
    supersedes_version_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_evidence (
    qa_version_id TEXT NOT NULL REFERENCES qa_versions(id),
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (qa_version_id, evidence_type, evidence_id)
);

CREATE TABLE IF NOT EXISTS bot_answers (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_message_id TEXT,
    telegram_bot_message_id TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    answer_mode TEXT NOT NULL,
    confidence REAL,
    qa_version_id TEXT,
    sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    bot_answer_id TEXT NOT NULL REFERENCES bot_answers(id),
    qa_id TEXT,
    reporter_hash TEXT,
    status TEXT NOT NULL,
    proposed_answer TEXT,
    admin_edited_answer TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
