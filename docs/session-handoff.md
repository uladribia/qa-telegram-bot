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
- The lexical projection is still present in production D1 (95 rows in
  `search_fts`). `migrations/0022_drop_search_fts.sql` drops it but has **not
  been applied**; that is a live DDL write and needs explicit authorization.

## The deployed configuration

| | |
|---|---|
| Embeddings | `@cf/google/embeddinggemma-300m` |
| Generation | `@cf/mistralai/mistral-small-3.1-24b-instruct` |
| Retrieval | one embedding, one cosine ranking, candidate pool 15 |
| Selection | top **3** Q&A + top **2** group candidates at or above the floor |
| Floor | `ANSWER_SIMILARITY_FLOOR=0.35` |
| Caps | 35 s deadline, 1024 max tokens, no `response_format` |
| Delivery | webhook acks `accepted` immediately, work runs in `waitUntil` |
| Cost | ~2.6-8 s and ~30 neurons per question, two model calls |

`ALLOWED_AI_MODELS` holds exactly these two models. Anything outside it is a
configuration error at startup. There is no lexical (BM25/FTS5) ranking and no
cross-encoder reranker; both were measured and removed.

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
- **The lexical leg was dead and was removed.** `_fts_query` AND-ed every query
  token including stopwords, so it returned rows for **2 of 91** eval questions
  and the "hybrid" was fusing one list. A fixed OR-of-content-tokens query was
  worth +2 questions in 43 at Recall@5. Gone with it: the `LexicalIndex` port,
  `D1LexicalIndex`, the RRF fuser, the lexical half of the search projection,
  and `search_fts` (migration `0022_drop_search_fts.sql`).
- **The evidence width is 3 + 2.** `QA_TOP_K` and `MESSAGE_TOP_K` are exposed
  next to the floor in `wrangler.jsonc` so the deployed width is readable in
  one place.

## What the width costs, measured on the 43 answerable eval questions

Gold `source_anchor` present in the Q&A handed to the model:

| | Recall@3 | Recall@5 | Recall@15 |
|---|---:|---:|---:|
| semantic only (shipped) | **0.860** (37/43) | 0.907 | 0.977 |
| with a *fixed* BM25 leg | 0.907 (39/43) | 0.930 | 0.977 |

One question (*"Com puc triar la meva talla de samarreta?"*) is not in the
pool at all. The five the top-3 cut loses are two at rank 4, two at rank 7 and
one at rank 11; a working lexical leg rescued two of those. **So the two
removals compound: no BM25 costs 2 questions, top-3 costs 2 more, and the
resulting recall is 0.860 rather than the 0.907 a working hybrid would have
given at the same width.** The gated local metric is Recall@3 (0.962 on the
synthetic set) because three is what ships; the former `Recall@5 >= 0.98` gate
described a five-candidate system and is reported but not gated. Restore it if
`QA_TOP_K` is raised.

## Verification

```text
make lint
make test              # 139 unit + architecture
make test-integration  # 126
uv run python -m evals.run offline   # 11/11
make eval-local        # all gates PASS, regenerates the report
```

Local retrieval with the leg removed: Recall@1 0.940, Recall@3 0.962,
Recall@5 0.970, MRR 0.953, against 0.984 / 0.986 / 0.973 with it and an
old-vector baseline MRR of 0.455.

**The live answer suite has not been run against this configuration.** The last
measured figure, 38/86, was at floor 0.45 with a reranker and a five-candidate
width. One run is ~1900 neurons.

## Open issues, in the order they will bite

1. **The generator is non-deterministic on borderline questions.** The same
   prompt over the same five documents returned `answered` in five offline
   reproductions and `abstention` in production. The live suite score is
   therefore noisy in both directions, and any single measurement of it is weak
   evidence. The fix is a model whose willingness to answer is stable, which has
   not been searched for.
2. **Retrieval recall is now the binding constraint, and it just got worse.**
   0.860 of answerable questions have their answer in the three candidates the
   model sees. The ceiling was already known; the width cut lowered it. The
   cheapest recovery is not a reranker — it is putting the question text *and*
   the answer text into whatever retrieval exists, or raising `QA_TOP_K` back
   and measuring what the extra candidates do to abstention.
3. **Completeness was never screened.** Mistral was chosen on grounding and
   declined 22 of 43 answerable questions at floor 0.45. A completeness screen
   means a rate over repeats, not a single call: 10 questions x 3 repeats x 3
   models is ~2700 neurons, one day. `llama-4-scout-17b-16e-instruct` is the
   untested candidate (3/3 grounded, 2.9 s, 31 neurons).
4. **No daily report arrives** until an external scheduler is wired to the
   route. This is the only broken thing left.

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

Four attempts at learned selection (answer relevance, pairing, cross-encoder
reranking, lexical fusion) passed their gates in isolation and failed in the
pipeline. The reranker is the clearest: 0.33 neurons and 0.49 s, a 1000:1
separation between relevant and irrelevant, and it made the bot worse because
it optimised ranking while the generator was starved of evidence. The lexical
leg is the second: a full port, adapter, projection and migration, returning
rows for 2 of 91 questions. Measure the signal in the position it will
actually be used, and measure the axis that is failing, not the one that is
easy to plot. Also: the *documentation* was the last thing to become true —
"hybrid retrieval, MRR 0.973" was repeated across four files and in a merge
commit hours after the leg was measured dead.
