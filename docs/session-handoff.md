# Session handoff

_Last updated: 2026-09-26, after the model swap, the reranker removal, and the
answer-floor recalibration._

## Current state

- `main` is merged and pushed. The deployed Worker runs the configuration below.
- The binding plan in `instructions/` is
  `qa-telegram-bot-retrieval-classifier-listener-plan.md` (completed). The work
  after it is not in that plan.
- One branch is deliberately **not merged**: `feat/pairing-head` (learned
  pairing head, disabled). See [experiments.md](experiments.md).

## The deployed configuration

| | |
|---|---|
| Embeddings | `@cf/google/embeddinggemma-300m` |
| Generation | `@cf/mistralai/mistral-small-3.1-24b-instruct` |
| Retrieval | semantic top-15 + BM25/FTS5 top-15, RRF fused (k=60) |
| Selection | Q&A top 5, messages top 4, `ANSWER_SIMILARITY_FLOOR=0.35` |
| Caps | 35 s deadline, 1024 max tokens, no `response_format` |
| Delivery | webhook acks `accepted` immediately, work runs in `waitUntil` |
| Cost | ~2.6-8 s and ~30 neurons per question, two model calls |

`ALLOWED_AI_MODELS` holds exactly these two models. Anything outside it is a
configuration error at startup.

## What shipped in this pass

**Diagnosis, not a bug hunt.** The user reported "the bot does not answer". The
webhook handler logged 37.2 s wall for a 46-character question against 466 ms of
CPU, so the time was a wait, not compute. The fix was to measure rather than
guess: one log line per Workers AI call (`operation`, `model`, `characters`,
`duration_ms`, `error`) settled in one request that the embedding call took
498 ms and the generation call took 35 000 ms and hit the deadline.

- **A reasoning model was the cause of the 37 s.** `glm-4.7-flash` spent 10 551
  characters of `reasoning_content` and 101 neurons to return `insufficient` on
  a question the knowledge base genuinely does not answer. Replaced with
  `mistral-small-3.1-24b-instruct` (2.5 s, 27 neurons), chosen because it was
  the only candidate of four that refused instead of inventing a payment method
  that is in no document.
- **Two caps with it:** `AI_GENERATION_MAX_TOKENS=1024` (answerable questions
  need 675-851 tokens) and no `response_format`.
- **The cross-encoder reranker was measured and removed.** `bge-reranker-base` is
  excellent in isolation and made retrieval worse in situ: reordering by
  relevance promoted low-cosine items and thinned the generation prompt to
  1047-1400 characters. Its score does not separate answerable from unanswerable
  questions at all — both distributions span 0.9997 to 0.0000.
- **The answer floor moved 0.45 to 0.35**, the lowest top-1 cosine among the 43
  answerable eval questions. It was a thickness knob set to a value that starved
  the generator; it was never a precision device (ten of twenty unanswerable
  questions already cleared 0.45).
- **The webhook now acknowledges before processing.** The pipeline used to run
  inside the request, so a 37 s handler overran Telegram's read timeout and
  Telegram retried the update. The answer still arrived by a separate
  `sendMessage`, which is why the symptom looked like answering and not
  answering simultaneously.
- **The Cron Trigger cannot work on the Free plan** and never did: 10 ms of CPU
  per cron invocation against a 3451 ms interpreter start-up. The report is
  delivered by an external scheduler calling `POST /internal/jobs/daily-report`.
- **Two stale eval labels were fixed.** `evals/answers.yaml` still expected
  `direct_qa` and `abstain`, both removed when answering became always
  grounded, so all 64 mode checks failed while `render()` truncated the output
  to eight lines. A dataset-validity exemption in `evals/run.py` was keyed on
  the same dead label.

## Verification

```text
make lint
make test              # 140 unit + architecture
make test-integration  # 127
uv run python -m evals.run offline   # 11/11
```

**The live answer suite has not been run against this configuration.** The last
measured figure, 38/86, was at floor 0.45 *with* the reranker. One run is
~1900 neurons.

## Open issues, in the order they will bite

1. **The generator is non-deterministic on borderline questions.** The same
   prompt over the same five documents returned `answered` in five offline
   reproductions and `abstention` in production. The live suite score is
   therefore noisy in both directions, and any single measurement of it is weak
   evidence. The fix is a model whose willingness to answer is stable, which has
   not been searched for.
2. **Completeness was never screened.** Mistral was chosen on grounding and
   declined 22 of 43 answerable questions at 0.45. A completeness screen means a
   rate over repeats, not a single call: 10 questions x 3 repeats x 3 models is
   ~2700 neurons, one day. `llama-4-scout-17b-16e-instruct` is the untested
   candidate (3/3 grounded, 2.9 s, 31 neurons).
3. **No daily report arrives** until an external scheduler is wired to the
   route. This is the only broken thing left.
4. **Retrieval recall is still the ceiling.** Short, conversational questions
   miss their gold anchor; no answer policy repairs that.

## Things that are not problems

- **Qwen is not a better Mistral here.** The only attractive Qwen generator,
  `qwen3-30b-a3b-fp8`, is reasoning-capable, which is exactly what cost 53 s
  and 100 neurons, and its CoT cannot be switched off through the
  OpenAI-compatible Workers AI endpoint. `qwq-32b` is explicitly a reasoning
  model; `qwen2.5-coder-32b-instruct` is a code model.
- **Retraining the learned heads costs nothing and is not worth doing.**
  `scripts/train_classifier.py` embeds through local Ollama and fits sklearn
  locally; serving is a coefficient matrix in the isolate. Zero Workers AI
  neurons either way. It is still not worth doing because
  `message_pair_candidates` and `feedback` are both empty — the earlier failure
  was missing labels, not model capacity.
- **The `unavailable` answer mode is now rare.** It means a Workers AI call
  failed, not that the bot is out of quota; a quota-exhausted question also
  degrades to it.

## The pattern to remember

Three attempts at learned selection (answer relevance, pairing, cross-encoder
reranking) passed their gates in isolation and failed in the pipeline. The
reranker is the clearest: 0.33 neurons and 0.49 s, a 1000:1 separation between
relevant and irrelevant, and it made the bot worse because it optimised ranking
while the generator was starved of evidence. Measure the signal in the position
it will actually be used, and measure the axis that is failing, not the one that
is easy to plot.
