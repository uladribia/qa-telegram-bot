-- SPDX-License-Identifier: MIT
-- Derived lexical (BM25) projection over Q&A ANSWER text, synchronized with
-- the vector projection. This is a rebuildable projection, never the source of
-- truth; SQL remains the truth and the vector store is rebuilt from it.
--
-- Why answer text and not the canonical question: the distinctive terms of this
-- knowledge base live in the answers. Measured on production before this
-- migration, "Cluber" appeared in 5 Q&A answers and 0 canonical questions, and
-- the venue entry's answer contains "camp" while its question does not. A
-- lexical index over questions cannot reach those facts at all.
--
-- The tokenizer is unicode61 with diacritics removed and NO stemmer: the only
-- stemmer FTS5 ships is Porter, which is English, and this corpus is Catalan.
-- Plural variants are handled by indexing as written.
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    vector_id UNINDEXED,
    kind UNINDEXED,
    scope_key UNINDEXED,
    canonical_key UNINDEXED,
    authority UNINDEXED,
    metadata_json UNINDEXED,
    answer_text,
    tokenize = 'unicode61 remove_diacritics 1'
);
