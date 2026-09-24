-- SPDX-License-Identifier: MIT
-- Separate semantic Q&A identity from source provenance.
--
-- The semantic canonical key is normalized by the maintenance command. The
-- exact web/source anchor remains on the immutable version that observed it.

ALTER TABLE qa_versions ADD COLUMN source_anchor TEXT;

ALTER TABLE qa_items RENAME COLUMN scope TO scope_key;
ALTER TABLE sources RENAME COLUMN scope TO scope_key;
