# Development

The canonical local mode is SQLite + NumPy + Ollama inside Docker. Host Python, uv, Ollama, and Cloudflare credentials are not needed for the documented local path.

## Test ladder

```bash
make lint
make test
make test-integration
```

`make test` is unit plus architecture and makes no network calls. `make test-integration` uses shared in-memory fakes. Run `make test-e2e-local` for changes affecting AI paths; it rebuilds the current Docker app image and runs the synthetic Telegram and local AI smoke flows inside the container.

Cloudflare live tests, remote reindexing, live evals, and real Telegram operations are explicit external acceptance steps and are never part of the offline loop. `.github/workflows/ci.yml` runs only the offline gates: lint, unit/architecture, integration, and offline evals.

## Synthetic evaluation datasets

The eval corpus contains four large offline datasets for experimentation:

- `evals/answers.yaml`: factual question variations with expected citation terms and `expected_mode`.
- `evals/classifier.yaml`: intent labels for question, knowledge update, correction, and chitchat variations.
- `evals/abstention_synthetic.yaml`: hard-negative and unsupported questions labelled `expected: abstain`.
- `evals/listener.yaml`: realistic WhatsApp-style windows for question detection and temporal Q→A pairing.

Synthetic cases are evaluation/training material only. They are never inserted into `data/seed/` or treated as club facts. `python -m evals.run offline` validates their structure without network or model calls.

## Local model evaluation results

A local-only pass was run with `embeddinggemma` and `gemma3:270m` against the expanded datasets. The judge used explicit rubrics for expected mode, required answer terms, forbidden claims, citations/evidence, classifier labels, question detection, and temporal pairs.

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

The dominant problem is retrieval/ranking quality, not forbidden claims. Bot self-knowledge currently competes with club Q&A in the same global index, causing many valid domain questions to abstain or retrieve the wrong entry. Classifier correction and knowledge-update recall are also weak. These are documented baseline results, not acceptance thresholds.

## Boundaries

`domain/` and `application/` contain no framework, Telegram, Cloudflare, or infrastructure imports. HTTP and Telegram adapters call explicit application services. SQL is the source of truth; vector projections are derived and repairable.

## Required checks before handoff

```bash
make format
make lint
make test
make test-integration
uv run python -m evals.run offline
```

The synthetic Telegram two-group flow is covered by the explicit local E2E command. Real Telegram two-group acceptance remains pending until a human runs it with test credentials. See [e2e-telegram.md](e2e-telegram.md).
