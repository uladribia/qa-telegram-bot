-- SPDX-License-Identifier: MIT
-- Retrieval is now cosine-only: one embedding, one threshold, no lexical leg.
-- As written the FTS5 query AND-ed every token including stopwords and
-- returned rows for 2 of 91 eval questions, so the projection was dead weight.
DROP TABLE IF EXISTS search_fts;
