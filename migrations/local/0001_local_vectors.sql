CREATE TABLE local_vectors (
    vector_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    status TEXT,
    canonical_key TEXT,
    object_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_local_vectors_kind_scope
    ON local_vectors(kind, scope_key);

CREATE INDEX idx_local_vectors_kind_status_scope
    ON local_vectors(kind, status, scope_key);
