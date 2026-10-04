# Clef-Flash focused decision evaluation

- experiment SHA: `fbeb094a73a6ec665826cd70e5d8b48f25d449b4`
- production runtime safety: listener traffic stayed on BaselineAssessmentModel; the endpoint persists nothing
- Clef configuration: @cf/cloudflare/clef-flash, one request per message, include_relevance=true, no fallback
- total Clef calls: 130 (3 served from cache)
- estimated neurons: 958 (cap 4500)

## Verdict

**CLEF_IS_WORTH_A_PRODUCTION_ROLLOUT_PLAN**

## Intent: 80 manual cases

| metric | baseline | clef-flash |
|---|---:|---:|
| accuracy | 0.7375 | 0.9250 |
| macro_f1 | 0.7368 | 0.9239 |
| question_recall | 0.7000 | 1.0000 |
| update_precision | 0.8889 | 0.9000 |
| correction_precision | 0.5000 | 0.8421 |
| chitchat_precision | 1.0000 | 0.9524 |
| confident_precision | 0.7647 | 0.9859 |
| coverage | 0.8500 | 0.8875 |
| error_rate | 0.0000 | 0.0000 |
| latency_p50_ms | 182.0000 | 230.0000 |
| latency_p95_ms | 292.0000 | 387.0000 |

## 50 hard windows

| metric | baseline | clef-flash |
|---|---:|---:|
| pair_precision | 1.0000 | 1.0000 |
| pair_recall | 0.4000 | 0.8800 |
| wrong_pairs | 0 | 0 |
| error_rate | 0.0000 | 0.0000 |

- primary result at the deployed 0.8/0.15: baseline pair precision 1.0000, clef 1.0000
- calibrated on 15 calibration cases: threshold 0.9, margin 0.05
- one-pass invariant: one request per message, asserted by the endpoint tests

## Per-category, clef-flash at the deployed thresholds

| category | expected | correct | wrong | missed |
|---|---:|---:|---:|---:|
| ambiguous_two_plausible | 0 | 4 | 1 | 0 |
| chitchat_noise | 0 | 5 | 0 | 0 |
| explicit_genuine | 5 | 5 | 0 | 0 |
| explicit_unrelated | 0 | 5 | 0 | 0 |
| multi_one_correct | 15 | 12 | 0 | 3 |
| no_correct | 0 | 8 | 2 | 0 |
| single_correct | 5 | 5 | 0 | 0 |

## 10 real listener scenarios

| metric | baseline | clef-flash |
|---|---:|---:|
| expected_pairs | 14 | 14 |
| made_pairs | 1 | 1 |
| wrong_pairs | 0 | 0 |
| pair_recall | 0.0714 | 0.0714 |

## Gates

| gate | result | detail |
|---|---|---|
| intent question recall (no worse than baseline by >0.02) | PASS | baseline 0.7000 vs clef 1.0000 |
| intent knowledge_update precision (<=0.02 worse) | PASS | baseline 0.8889, clef 0.9000 |
| intent correction precision (<=0.02 worse) | PASS | baseline 0.5000, clef 0.8421 |
| intent confident precision (<=0.01 worse) | PASS | baseline 0.7647, clef 0.9859 |
| intent coverage (<=0.05 worse) | PASS | baseline 0.8500 vs clef 0.8875 |
| intent error rate < 1% | PASS | clef 0.0000 |
| 10 real scenarios: 0 wrong pairs (clef) | PASS | baseline 0 wrong, clef 0 wrong |
| held-out pair precision >= 0.98 | PASS | baseline 1.0000, clef 1.0000 |
| held-out wrong pairs <= 1 | PASS | clef 0 |
| no_correct: >= 6/7 | PASS | clef 8/7 |
| ambiguous_two_plausible: >= 3/4 | PASS | clef 4/4 |
| explicit_unrelated: >= 3/4 | PASS | clef 5/4 |
| chitchat_noise: >= 3/3 | PASS | clef 5/3 |

- safety gates passed: True
- value gate, multi-question recovery: True
- value gate, held-out recall gain: True (gain 0.4800)
