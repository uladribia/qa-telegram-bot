# Session handoff

_Last updated: 2026-09-27, after the failure-attribution, Loguru-removal, and
eval-consolidation pass._

## Current state

- `main` is merged and pushed. The deployed Worker runs the configuration below.
- **Every answer outcome now carries one semantic reason**: `answered`,
  `no_evidence`, `model_insufficient`, `invalid_model_output`,
  `invalid_source_ids`, or `model_unavailable`. A malformed model reply is no
  longer reported as a model abstention; it raises
  `InvalidModelOutputError` with a safe code and is recorded as
  `invalid_model_output`. `AnswerService.decide` is the only mapper.
- **Loguru is gone.** The standard library is the one logging system,
  configured in `infrastructure/logging.py`: stderr stays human-readable
  locally and becomes one JSON object per line in the Worker, and the same
  records are forwarded to Logfire.
- **The answer pipeline is traced** with `answer_question` → `retrieval` →
  `evidence_selection` → `generation` → `answer_decision`, in both runtimes
  and through the evaluation path too. `answer_question.reason` is the same
  value as `refusal_reason` in `trace_json`.
- **Content capture is a gated testing mode.** `KB_LOGFIRE_CAPTURE_CONTENT`
  (default `true`) exports message text, sender identity, prompts, and
  answers so a flow can be reconstructable; `KB_LOGFIRE_SEND_TO_LOGFIRE` is
  the separate switch for leaving the process. Credentials are scrubbed in both
  modes. Tests set the send switch to `false` in `tests/conftest.py`.
- **The evals have one source of truth.** `evals/gold.yaml` (11 answers + 15
  abstentions, human-checked) is the live gate; `evals/frozen_generation.yaml`
  (30 cases) measures the generator with retrieval bypassed; synthetic sets are
  experiment material and are not in the default gate. `must_include` is a
  hard failure now, and both arbitrary size gates are gone.
- **Production tracing is off** because of the isolate memory limit, not for
  lack of a token. The local runtime is the only one exporting today.
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
make test              # 159 unit + architecture
make test-integration  # 139
uv run python -m evals.run offline   # 11/11
make eval-local        # local Ollama + SQLite quality gate
make smoke             # Worker boots, /healthz 200
```

**The live suites have not been run against the deployed Worker.** They need
`ALLOW_CLOUDFLARE_LIVE_TESTS=1` and a `BOT_BASE_URL`, and they burn the shared
daily budget. `make eval-live-frozen` is the one to run first: it is the only
suite that isolates a single component, at one generation call per case, and it
is the cheapest way to tell a generator problem from a retrieval problem.

`make eval-local` passes all its gates (classifier, retrieval, listener), but
none of those gates measure answer quality — see the finding above, the local
generator is currently broken.

`make smoke` asserts `/healthz`, which resolves no context, so it does **not**
cover the observability path. After touching it, POST a webhook at a booted
Worker and read the logs: that is how the `instrument_httpx` failure that 500'd
the first real request was found.

Local retrieval with the leg removed: Recall@1 0.940, Recall@3 0.962,
Recall@5 0.970, MRR 0.953, against 0.984 / 0.986 / 0.973 with it and an
old-vector baseline MRR of 0.455.

**The live answer suite has not been run against this configuration.** The last
measured figure, 38/86, was at floor 0.45 with a reranker and a five-candidate
width. One run is ~1900 neurons.

## qwen3-30b-a3b in production, compared with mistral

Run 2026-09-27 on `eval/qwen3-prod-comparison`, both suites in production.
`/no_think` works: 3-4 s per call, valid JSON, no timeout. Production was
restored to mistral afterwards and re-verified.

| | mistral | qwen3 + /no_think |
|---|---|---|
| frozen cases earned | 22/30 | 23/30 |
| gold cases earned | 20/26 | 21/26 |
| unreadable replies | 1 | 2 |
| metered cost per call | ~9 | ~18 |

Both models fail **the same five gold cases**; two differ only in kind, not in
outcome. Quality is a wash, qwen costs about twice as much on a meter that is
known to be model-blind, and its JSON compliance is slightly worse. Nothing here
justifies a switch, and the answer is the same whichever way the granite
comparison went.

Worth keeping: `WorkersAIGenerator(no_think=True)`, wired to
`AI_APPEND_NO_THINK`, default off and inert for non-Qwen models, with a unit
test asserting the token is only sent when asked for. It is the only thing from
this experiment worth carrying, because the model will need it if it is ever
adopted.

## glm-4.7-flash, thinking disabled: the final model decision

The model rejected in September for spending 53 s and 101 neurons on one
`insufficient`. Retested 2026-09-27 with `chat_template_kwargs.enable_thinking
= false`, which Cloudflare exposes and GLM honours: **3.3-3.9 s per call, valid
JSON, no timeout.** The deliberation is fully gone.

| | mistral | qwen3 + /no_think | glm-4.7 thinking off |
|---|---|---|---|
| frozen earned | **22/30** | 23/30 | 21/30 |
| gold earned | **20/26** | 21/26 | 20/26 |
| metered per call | **~9** | ~18 | ~25 |

All three fail **the same five gold cases**. Those are knowledge and grounding
problems, not model problems. **Decision: stay on mistral-small-3.1-24b-instruct.**
Cheapest, no thinking switch needed, and the model swap would buy at most one
case. Production was restored to mistral and re-verified.

Kept from the experiment: `WorkersAIGenerator(disable_thinking=True)` wired to
`AI_DISABLE_THINKING`, default off, with a test asserting
`chat_template_kwargs` is only sent when asked for. If a reasoning model is ever
adopted, that switch is what makes it fit the deadline — it is the same lever
that rehabilitated GLM here.

Today's meter after all three model runs: ~5,277 of 10,000, so the eval ceiling
(7,500) is still open but the real quota is the binding constraint, not ours.

## The gold set, run on both runtimes

The suite that measures this knowledge base, run 2026-09-27 on both runtimes:

| | local | production |
|---|---|---|
| assertions | 15/26 (58%) | 75/80 (94%) |
| **cases earned** | **0 / 26** | **20 / 26** |

Local earns nothing, for the same reason as the frozen suite: unreadable
replies, not decisions. Production's five failures are three distinct
problems, and the distinctions matter more than the count:

- **3 false abstentions** (`equipment_when`, `equipment_size`, `medical_expiry`):
  evidence was retrieved above the floor and the model declined anyway. Cheap to
  re-measure, worth a look at whether the floor is too generous for the
  generator's willingness to answer.
- **1 retrieval miss answered confidently** (`training_where`): "On entrenen?"
  retrieved the rain policy and the model answered *that*, fluently. This is the
  most serious finding in the batch, and it is retrieval-side: the previous
  recall work optimised for the right document appearing at all, not for the
  right document being the one the model uses.
- **1 wrong-entity answer** (`gold_abstention_07`): asked for a delegate's
  phone, answered with the club's published general line. Not a leak, the number
  is public in `qa-contacte-club`; wrong because it answers about a different
  entity. Whether the bot should instead offer the published contact is a
  product question nobody has answered yet, and the case keeps failing until
  someone does.

Production also produced one unreadable reply in this run (one accidental pass),
confirming it is not only the local model.

## The frozen suite, run on both runtimes

Same dataset, same code, same runner, one generation call per case, run on
2026-09-27 against the deployed Worker and the local stack:

| | local | production |
|---|---|---|
| model | `gemma3:270m` (Ollama) | `@cf/mistralai/mistral-small-3.1-24b-instruct` |
| assertions | 80/194 (41%) | 149/165 (90%) |
| **cases earned** | **0 / 30** | **22 / 30** |
| failed | 30 | 8 |

The assertion rate flatters local. Nine local abstention cases satisfy their
mode assertion, but the reason is `invalid_model_output`, not a decision, so
the report counts them as accidental and local earns **none** of its 30 cases.
The report now splits every suite into earned, accidental, and failed, and
`--json` emits the same per case.

The eight production failures are generator behaviour, none retrieval: two
false abstentions on a multi-part and a conditional question, one **false
answer** where abstention was required, two incomplete citations, two content
misses, and one unreadable output. Multi-part, conditional, and partial-support
shapes are the weak spots. Two consecutive runs failed a slightly different
subset, so re-run before concluding anything about one case.

`make eval-frozen-local` (no authorization needed) and `make eval-live-frozen`
(explicit opt-in) run the same suite against the two targets.

The six remaining production failures are all generator behaviour, none
retrieval: two false abstentions (`two_sources_needed_together`,
`conditional_answer`), one incomplete citation where a stated fact was not
cited (`three_sources_only_two_answer`), one content miss
(`correction_rejects_outdated_detail`), and the two citation assertions of the
first case. One earlier run produced `invalid_model_output` on a case that
passed on the re-run, so production also emits unreadable JSON occasionally.

One case in the dataset was wrong and was fixed: `single_sufficient_source`
forbade the word "dissabtes", which its own evidence says is closed, so a
faithful answer tripped the check. A forbidden phrase must be something the
evidence does not support.

## Production deployment state

Deployed `a86453e1-3330-4201-adac-f28d7181a341` with the reason taxonomy, the
semantic spans, and the frozen endpoint. **`KB_LOGFIRE_ENABLED` is `false`**:
with the Logfire SDK enabled the Worker returns *Worker exceeded resource
limits* on the first request that resolves a context, because the 128 MB
isolate limit cannot hold the OpenTelemetry SDK and its protobuf exporter.
`/healthz` and `/readyz` answer 200 either way, which is why smoke did not
catch it. A `LOGFIRE_TOKEN` secret is set and unused, so tracing is one flag
away once a memory-compliant transport exists. Details in
[operations.md](operations.md#production-tracing-is-disabled-and-why).

## The finding this pass produced

**The local generation model cannot answer anything, and until this pass that
was invisible.** Measured against the real local stack on 2026-09-27:

```text
gemma3:270m       -> InvalidModelOutputError(schema_validation)
                     raw='{"status": "answered"}'
granite4:micro-h  -> OK  status=answered answer='La botiga obre de 10:00 al 20:00.'
```

`gemma3:270m` returns an object with a `status` and nothing else, which fails
`GenerationOutput` validation. Every question therefore ended in
`invalid_model_output`, whatever the evidence was. Before this pass the same
event was recorded as a model abstention and read as a knowledge or retrieval
problem; the frozen suite now names it in one line
(`invalid_model_output=30`).

This is a local-runtime problem, not a production one: production runs
`mistral-small-3.1-24b-instruct`, which does answer. `granite4:micro-h` handles
the schema correctly but is **not** in `LOCAL_ALLOWED_AI_MODELS`, and the
zero-cost policy correctly refused it when tried; changing the local model is a
separate, explicitly authorized decision, not part of this pass.

Consequence for the local loop: any local measurement of answer quality is
currently a measurement of a broken generator. Fix the local model before
trusting `make eval-local` for anything about answers.

## Open issues, in the order they will bite

1. **The local generation model is broken** (above). Every local answer ends in
   `invalid_model_output` with `gemma3:270m`. Either allow and adopt a local
   model that honours the schema, or stop trusting local answer measurements.
   This blocks every other local quality question.
2. **No live suite has run against the production models.** The gold and
   frozen suites need an authorized live run; the frozen suite has only been
   run against the local stack, where the generator is the problem above.
3. **The generator is non-deterministic on borderline questions.** The same
   prompt over the same five documents returned `answered` in five offline
   reproductions and `abstention` in production. The live suite score is
   therefore noisy in both directions, and any single measurement of it is weak
   evidence. The fix is a model whose willingness to answer is stable, which has
   not been searched for.
4. **Retrieval recall is now the binding constraint, and it just got worse.**
   0.860 of answerable questions have their answer in the three candidates the
   model sees. The ceiling was already known; the width cut lowered it. The
   cheapest recovery is not a reranker — it is putting the question text *and*
   the answer text into whatever retrieval exists, or raising `QA_TOP_K` back
   and measuring what the extra candidates do to abstention.
5. **Completeness was never screened.** Mistral was chosen on grounding and
   declined 22 of 43 answerable questions at floor 0.45. A completeness screen
   means a rate over repeats, not a single call: 10 questions x 3 repeats x 3
   models is ~2700 neurons, one day. `llama-4-scout-17b-16e-instruct` is the
   untested candidate (3/3 grounded, 2.9 s, 31 neurons).
6. **No daily report arrives** until an external scheduler is wired to the
   route. This is the only broken thing left.
7. **The Worker cannot host the Logfire SDK.** It exceeds the 128 MB isolate
   limit on import, which 503s every real question. The Worker therefore runs
   with `KB_LOGFIRE_ENABLED=false` and exports nothing. A memory-compliant
   transport is the only way to get production traces; the token and the
   wiring are already in place behind the flag.

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

Five attempts at learned selection (answer relevance, pairing, cross-encoder
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

The same pattern showed up in observability three times, and the third one cost
a production incident. `/healthz` answering 200 was read as "the Worker is
fine" while the first real request 500'd on a missing client library, because
the health check resolves no context. "A clean exporter log" was read as
ingestion until a query returned the span. Then the tracing pass passed
`make smoke`, deployed, and returned *Worker exceeded resource limits* to every
real question — again only a route that resolves a context would have shown it,
and again only a live probe did. A gate that does not execute the path is not
evidence about the path, and a deployment is the last place to discover it.

The companion lesson is from the eval: the frozen suite reported
`invalid_model_output=30` locally and `90%` in production in one run each. The
number was the whole finding. Two teams reading the same abstention rate in
their logs would have argued about knowledge for a week.
