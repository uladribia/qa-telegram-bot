# Session handoff

_Last updated: 2026-09-24 on `chore/eval-handoff`._

## Current state

- Binding plan: [`instructions/qa-telegram-bot-one-pass-final-fix-plan.md`](../instructions/qa-telegram-bot-one-pass-final-fix-plan.md).
- Current branch before merge: `chore/eval-handoff`.
- `main` was at `f5224fe` before this evaluation pass.
- The untracked plan file is preserved and must not be staged or discarded.
- Local Docker services are running with the current checkout image.

## Work completed

- Hardening branch merged into `main` and pushed.
- Cloudflare Worker deployed at `https://bhc-qa-testbot.qa-bots.workers.dev`.
- D1 migration `0020_final_hardening.sql` applied; no remote migrations remain.
- `/healthz` and `/readyz` returned `200` after deployment.
- Real Telegram manual acceptance remains pending.
- Local Docker database was reset and reseeded with `data/seed/qa.json` and `data/seed/bot_self_qa.json` for a clean local evaluation baseline.

## Evaluation datasets

The following datasets are present and validated by `python -m evals.run offline`:

- `evals/answers.yaml`: 76 factual answer and abstention cases.
- `evals/classifier.yaml`: 222 intent-classifier cases.
- `evals/abstention_synthetic.yaml`: 294 synthetic abstention hard negatives.
- `evals/listener.yaml`: 60 realistic WhatsApp-style listener windows with expected questions, answers, and pairs.
- `docs/manual-test-questions.md`: 57 manual Q&A questions, including source-anchor expectations and safety checks.

Synthetic cases are evaluation/training material only. They are not production seed knowledge.

## Latest local model baseline

Models: `embeddinggemma` and `gemma3:270m`, local only. Throwaway rubric judge checked answer mode, required terms, forbidden claims, citations/evidence, classifier labels, question detection, and temporal pairs.

| Area | Metric | Result |
|---|---|---:|
| Answers | Mean rubric score | 37.1/100 |
| Answers | Pass rate ≥80 | 26.3% |
| Answers | Mode accuracy | 31.6% |
| Answers | Citation/evidence validity | 31.6% |
| Answers | Required-term coverage | 27.6% |
| Answers | Forbidden-claim clean rate | 100% |
| Retrieval | Recall@1 | 15.2% |
| Retrieval | Recall@3 | 27.3% |
| Retrieval | Recall@5 | 39.4% |
| Retrieval | MRR | 0.224 |
| Classifier | Label hit rate | 52.3% |
| Classifier | Strict single-label match | 51.8% |
| Listener | Question recall | 51.7% |
| Listener | Non-question false-positive rate | 28.8% |
| Listener | Pair precision | 65.2% |
| Listener | Pair recall | 90.6% |
| Listener | Pair F1 | 75.8% |

The main quality problem is retrieval/ranking. Bot self-knowledge competes with club Q&A in the same global index, producing incorrect rankings and excessive abstentions. The classifier also has weak knowledge-update and correction recall. The direct-answer threshold should not be lowered without measuring wrong-answer precision.

Retrieval output now prefers `source_anchor` metadata, which fixes evaluation identity reporting but does not by itself solve ranking quality.

## Validation

```text
make lint       ✅
make test       ✅ 120 passed
offline evals   ✅ 9/9 suites passed
make test-e2e-local ✅ local Docker AI smoke passed earlier
```

## Remaining work

1. Commit and merge this evaluation/documentation pass into `main`.
2. Improve retrieval/ranking before changing answer thresholds.
3. Improve question classification and reduce listener false positives.
4. Reduce temporal pair over-generation without losing the current 90.6% pair recall.
5. Run the manual two-group Telegram acceptance flow.
6. Only then perform any explicitly authorized production reindex or Cloudflare evaluation.

Read `AGENTS.md` and the binding plan before continuing. Preserve the untracked plan file and do not discard local Docker volumes unless explicitly requested.
