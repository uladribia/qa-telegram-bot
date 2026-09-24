-- SPDX-License-Identifier: MIT
-- Derived vector projection manifest.

CREATE TABLE search_projection (
    vector_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
