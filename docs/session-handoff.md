# Session handoff

_Last updated: 2026-09-25, after the always-grounded answering release._

## Current state

- `main` is merged and pushed at `09d616b`, with `v1.1.0` tagged.
- The deployed Worker runs the always-grounded answering path with
  `ANSWER_SIMILARITY_FLOOR=0.45`.
- One branch is deliberately **not merged**: `feat/pairing-head` (learned
  pairing head, disabled). See [experiments.md](experiments.md) and "Parked
  work" below.
- The binding plan in `instructions/` is
  `qa-telegram-bot-retrieval-classifier-listener-plan.md` (merged and
  completed). The pairing-head work extends it but is not in that plan.

## What shipped since the last handoff

**Retrieval, classifier, listener** (plan `qa-telegram-bot-retrieval-classifier-listener-plan.md`):

- Question-focused embeddings (Q&A embeds the question only, paired messages
  embed their context question).
- Hybrid retrieval: semantic top-10 plus BM25/FTS5 top-10, fused with RRF, with
  local-canonical override and authority tie-break. Local Recall@5 0.986, MRR
  0.973 against a 0.455 baseline.
- A locally trained linear intent head (4 labels, 768 features) replacing
  prototype scoring. Macro F1 0.964, confident precision 0.989 at 0.918
  coverage. Zero extra AI calls.
- A conservative deterministic listener: explicit replies pair immediately,
  otherwise exactly one plausible question inside 5 minutes. No model call.
- Two production bugs found and fixed while releasing: a Worker isolate cannot
  read repository files (the head now ships as an embedded module), and
  `Vectorize.deleteByIds` caps at 100 ids (bulk deletes are chunked).

**Answer selection: always grounded in the model.** The verbatim-echo path is
gone. Every answered question goes to one grounded generation call with the Q&A
items that clear a single floor, and the bot abstains when nothing clears it or
when the model says so. This was chosen because the echo answered 98.7% of
unknown questions with the nearest document's answer; the model declines those
instead. `ANSWER_SIMILARITY_FLOOR` was calibrated in production from 0.70 to
0.45 because the local floor curve did not transfer to short real questions.

**Operational hardening:** `/healthz` is served by the static asset layer so
liveness does not depend on the Python interpreter; the evaluation endpoints
cap queries per request and throttle per isolate; live evals run one suite at a
time with a `--suite` selector.

## Verification

```text
make lint
make test              # 131 unit + architecture
make test-integration  # 126
uv run python -m evals.run offline   # 11/11
make test-e2e-local    # 4 (Docker + local Ollama)
```

Production was verified live after deploy: an answerable question returns a
cited synthesis, an unknown question abstains, and the floor behaves at 0.45.

## Parked work

`feat/pairing-head` (unmerged, not deployed):

- Persisted question embeddings (migration `0022_pairing_embeddings.sql`,
  nullable `intent_embedding` BLOB) so pairing features cost no extra model
  call across stateless isolates.
- `application/pairing_head.py` with 13 features and a relative policy
  (tau plus a margin over the runner-up).
- `scripts/train_pairing.py`, 10 unit tests, and
  `uv run python -m evals.pairing` (24 deterministic scenarios).

Measured: the head passes its gate on synthetic held-out pairs (precision
0.975) but does **not** transfer to realistic scenarios, where it pairs less
often than the deterministic rule at the same precision. The finding and the
reason are documented in [experiments.md](experiments.md). Merging it would add
an unused head and a migration; keeping it unmerged is a deliberate choice.

## The pattern to remember

Two attempts at learned selection (answer relevance, pairing) both passed their
gates on synthetic data and both failed on realistic, short, Catalan club
messages. Synthetic-only training does not transfer to this corpus. The missing
ingredient is **real labelled data**, which the system already records:

- `message_pair_candidates`: audit rows for every pair the listener accepted.
- human-approved corrections and the gold Q&A anchors: answerable labels.

## Next steps, in order

1. Harvest labels from the audit rows and approved corrections once a group has
   real traffic; retrain the pairing head (and any future selector) on those,
   with the synthetic set as pre-training only.
2. Retrieval recall on short questions is the other known ceiling: only 27 of
   47 labelled answer cases surface their anchor. This bounds answer quality
   regardless of the answer policy.
3. Watch the `unavailable` rate in production: every question now reaches the
   model, so transient model failures are visible on all traffic.
4. Re-run the live retrieval suite in small pieces (100 queries at a time) now
   that the eval endpoints are capped; the earlier full run was interrupted by
   Worker 503s.

## Operational notes

- The eval load test wedged the Worker twice (all requests failing with a
  Pyodide `Cannot enter a promising task` error at interpreter init). A plain
  `npx wrangler deploy` recovered it both times. If `/healthz` is green but
  `/readyz` and authenticated endpoints return 500/1101, redeploy before
  debugging code.
- Today's usage is well inside the 10,000-neuron daily budget. An answered
  question costs about 102 neurons (about 76 with three evidence items);
  abstaining costs about 0.9.
- Deploying requires the new `0022_pairing_embeddings.sql` migration only if
  the pairing branch is ever merged; `0021_search_fts.sql` is already applied.
