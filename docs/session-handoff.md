# Session handoff

_Last updated: 2026-10-04, after shipping the decision model for listener
intent, answer sufficiency and evidence selection._

## The state, in one paragraph

A decision model (`@cf/cloudflare/clef-flash` through the Workers AI binding)
now decides three things in production: what an unaddressed group message is,
whether the retrieved shortlist can answer a question at all, and which items
in it do. Retrieval still picks the shortlist and the cosine floor still bounds
it, so the model decides correctness, never recall or tokens. `main` is merged
and pushed, the Worker is deployed and answering, and the local stack runs the
same code path against a stand-in model.

## What the measurements were

All numbers below are held-out, with thresholds chosen on a calibration split
and applied once. Reports: `reports/clef-flash-focused-eval.md`,
`reports/clef-answer-decisions.md`.

| decision | before | after | note |
|---|---|---|---|
| listener intent (80 manual) | macro F1 0.737 | 0.924 | question recall 0.700 → 1.000 |
| listener intent, real scenarios | 1 of 14 pairs | 3 of 14 | zero wrong pairs either way |
| answer sufficiency | no decision existed | false answers 1.000 → 0.000 | 0.394 headroom |
| evidence selection | all 7 items passed | 11% passed | right item 1.000 |
| abstention coverage | 1.000 | 0.913 | 8 of 92 answerable now abstain |

Shipped thresholds: `DECISION_SUFFICIENCY_THRESHOLD=0.50`,
`DECISION_SELECTION_THRESHOLD=0.90`. Both were chosen on calibration; changing
one means re-running `make eval-clef-production`, not guessing.

## Three things that were measured and deliberately NOT shipped

1. **Model-chosen pairing** (`DECISION_INCLUDE_RELEVANCE`): recall rose 0.41 →
   1.00 on the windows, but it produced 3 wrong pairs, 2 of which turned out to
   be defects in our own dataset. Verdict `KEEP_CURRENT`; the code is wired and
   tested, off by default.
2. **The old linear classifier still exists as the rollback and is still bad.**
   Its own gate fails (macro F1 0.594 locally). It is no longer on the intent
   path in production, which is the point, but do not treat it as a quality bar.
3. **Direct verbatim answers** were dropped at the user's decision: every answer
   still goes through the generator.

## What is deployed right now

```
worker        dd98d57c-f785-479b-9fd6-e1fa9ddd2817
health        /healthz ok · /readyz ok
webhook       POST /adapters/telegram/webhook (405 on GET means it is live)
budget        852.9 of 10,000 neurons today
```

Verified live after the deploy: an intent case answers `question` at 0.9475, and
an answer case returns sufficiency 0.853 with exactly one of two evidence items
selected — the decision the shipped thresholds produce.

## The knobs, and which way each one turns

```text
DECISION_BACKEND=systemone        the shipped path; `baseline` is the rollback
DECISION_ANSWER_PATH=true         sufficiency + selection after retrieval
DECISION_INCLUDE_RELEVANCE=false  pairing stays with the deterministic policy
DECISION_FALLBACK_TO_BASELINE=true  a decision failure degrades to the floor
AI_DECISION_TIMEOUT_SECONDS=8     a decision is not a generation; it is not waited on
```

Rollback is one variable and needs no deploy if it is already in the
environment: `DECISION_BACKEND=baseline`. That state is the one the project
shipped until today, so it is well understood.

## Known limits, stated plainly

- **Coverage cost**: 8.7% of answerable questions now abstain where they would
  have been answered. Raising sufficiency buys coverage and spends safety.
- **Latency**: about 300 ms p50 per answered question, on top of generation.
- **The abstention dataset is easier than reality.** The 15 human abstentions are
  the honest part and top out at sufficiency 0.106, but 15 cases cannot carry a
  production guarantee.
- **Local decisions are a stand-in.** `tev1:0.8b` exercises the code path;
  `reports/decision-service.md` says nothing about the shipped model.
- **Two bugs found and fixed in this work**, both by tests that now exist: the
  listener parser rebuilt decision names from candidate ids and broke the
  answer-shape decisions; and the generator asked for a schema whose defaulted
  fields were optional, which a small model answered with a bare status.

## Next session, in order of value

1. **Watch real abstentions.** The `evidence_selection` span carries
   `decided_by` and `abstained`; a week of those is the first real evidence
   about the 8.7%.
2. **Re-run `make eval-clef-production` only if a threshold or the model
   changes.** It is one pass, cached, and costs neurons.
3. **The decision evaluation endpoint can now be removed** if you want the
   surface gone; it is the only route that can build the un-metered evaluation
   model, and `tests/integration/test_internal_eval_decision.py` pins that user
   traffic cannot reach it.
4. **Still open from earlier**: the classifier gate failure predates all of this
   and is now only a rollback-quality question, not a production one.

## Rites

```bash
make all                    # lint + fast tests
make test-integration       # in-process flows
make test-e2e-local         # local stack including the decision service
make decision-smoke         # the local decision service answers one canonical request
```

Live runs need explicit authorization: `ALLOW_CLOUDFLARE_LIVE_TESTS=1`,
`BOT_BASE_URL`, and for the decision evaluation `INTERNAL_ADMIN_KEY`. Never
scheduled, never auto-retried.
