-- SPDX-License-Identifier: MIT
-- Derived lexical (BM25) projection synchronized with the vector projection.
-- This is a rebuildable projection, never the source of truth.

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    vector_id UNINDEXED,
    kind UNINDEXED,
    scope_key UNINDEXED,
    canonical_key UNINDEXED,
    authority UNINDEXED,
    metadata_json UNINDEXED,
    question_text
);
