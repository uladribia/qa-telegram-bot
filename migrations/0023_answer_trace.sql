-- SPDX-License-Identifier: MIT
-- Debug trace of one answer attempt, stored with the answer it explains.
-- The worker has no log history, so the only durable record of why an answer
-- was produced or refused is this column. It holds sizes, ids, similarities,
-- statuses and durations -- never the question text, the evidence text, the
-- prompt, or the model output, which stay out of storage per the logging rules.
ALTER TABLE bot_answers ADD COLUMN trace_json TEXT NOT NULL DEFAULT '{}';
