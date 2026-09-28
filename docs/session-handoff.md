# Session handoff

_Last updated: 2026-09-28, after the evidence-question, seed, and lexical-leg
pass._

## Read this first: production is down

**Every Workers AI call has been failing since ~05:54 UTC on 2026-09-28, so the
bot cannot answer anything.** `model_unavailable` with an empty evidence set on
every question, and `POST /internal/smoke/runtime` — a direct AI call with no
retrieval — returns 500. The worker log shows `workers_ai_call_failed` on the
embedding call with `"error":"JsException"` at `duration_ms: 0.0` and no
`workers_ai_call_completed` events at all. The app's `ai_budget` table is frozen
at 1 981 neurons across every attempt, so the calls fail before being metered:
the platform is refusing them, not the local guard (which sits at 7 500).
`/healthz` still answers 200 because it resolves no context and never touches
AI. **There is no fallback by design** — the zero-cost policy forbids an
alternative provider. Re-run `make eval-live-gold` to confirm recovery before
trusting any measurement taken while this was open. It is **not** caused by the
lexical revert, which only sets `QA_ANSWER_TOP_K=0` and returns from the leg
before any query.

## The model decision, in one line

`@cf/zai-org/glm-4.7-flash` **with thinking disabled** now ships, reversing the
mistral decision on completeness grounds: six consecutive abstentions on 09-26
and two on 09-27 with complete evidence. `AI_DISABLE_THINKING=true` is
**mandatory** — without it this is the 53-second timeout that started all of
this. Accepted knowingly: ~25 neurons a call against mistral's ~9, 21/30 frozen
against 22/30, and a new `redundant_sources` failure. Numbers in
[operations.md](operations.md#the-generator-measured-on-both-runtimes).

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
- **The generator now sees the question each answer was written for.** Evidence
  renders as `Q:` (the question) and `A:` (the text, every line prefixed). The
  question used to be dropped at the `EvidenceItem` port boundary, so the model
  received bare assertions. The `A:` prefix also fixed a real ambiguity:
  multi-line answers used to run into the next item with only the `[id]` header
  marking a boundary.
- **Provenance reaches the prompt** as `official` or `reported`, plus the
  connector-declared `source_kind`, which had been indexed and never read.
  Official outranks reported on conflict, and rule 4 keeps authority from ever
  licensing an answer.
- **`data/seed/qa.json` is now tracked** (it was gitignored, so the repo could
  not rebuild what production was running). The WhatsApp imports and
  `data/raw/*` stay ignored.
- **`kb promote` exists for seeded `under_review` Q&A.** That state was
  write-only: retrieval filters on `active`, nothing embedded it, and no
  feedback record existed to approve it, so it could never answer. Two
  production entries were stuck that way. Approving a reviewer correction does
  not need it — that path already writes `active`.
- **The BM25 leg is merged but disabled** (`QA_ANSWER_TOP_K=0`). It measured
  neutral-to-negative on the live gold set, so it does not run; the code, the
  migration and the FTS projection stay so it can be revived behind a generator
  fix. One branch is deliberately not merged: `feat/pairing-head` (learned
  pairing head, disabled, see [experiments.md](experiments.md)).

## The deployed configuration

| | |
|---|---|
| Embeddings | `@cf/google/embeddinggemma-300m` |
| Generation | `@cf/zai-org/glm-4.7-flash` with `AI_DISABLE_THINKING=true` |
| Retrieval | one embedding, one cosine ranking, candidate pool 15 |
| Selection | top **5** Q&A + top **2** group candidates at or above the floor |
| Floor | `ANSWER_SIMILARITY_FLOOR=0.35` |
| Lexical leg | `QA_ANSWER_TOP_K=0` — **disabled**, see the finding below |
| Caps | 35 s deadline, 1024 max tokens, no `response_format` |
| Delivery | webhook acks `accepted` immediately, work runs in `waitUntil` |
| Cost | ~7-9 s and ~25 neurons per question, two model calls |

`ALLOWED_AI_MODELS` now holds the embedder, mistral, and glm-4.7-flash. Anything
outside it is a configuration error at startup. There is no cross-encoder
reranker; one was measured and removed.

`search_fts` **does** exist in production D1 again, with 62 rows, created by
`migrations/0024_answer_fts.sql`. The leg that reads it is off.

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
- **The evidence width is 5 + 2.** `QA_TOP_K` and `MESSAGE_TOP_K` are exposed
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

**The old 76-case live answer suite no longer exists.** It was consolidated into
`evals/gold.yaml` (11 answerable + 15 abstentions) when the plan's Phase 6 landed,
so the historical 38/86 figure has no comparable successor: it was measured at
floor 0.45, with a reranker and a five-candidate width. The current measurement
is the gold suite's 20/26 cases earned, which is a different instrument and
should not be read as an improvement on 38/86. The 65-case synthetic answer
material now lives in `evals/answers_synthetic.yaml` for local experiments only.

## Production model comparisons

Three models were run in production on 2026-09-27, both suites, same cases, one
variable. Canonical numbers and method:
[experiments.md](experiments.md#generation-model-re-measured-the-axis-that-was-missing-now-measured).

| | mistral | qwen3-30b-a3b (`/no_think`) | glm-4.7-flash (thinking off) |
|---|---|---|---|
| frozen cases earned | **22/30** | 23/30 | 21/30 |
| gold cases earned | **20/26** | 21/26 | 20/26 |
| metered cost per call | **~9** | ~18 | ~25 |
| needs a thinking switch | no | yes | yes |

All three fail the same five gold cases, which is what makes those five
knowledge defects rather than model defects. That comparison was run with
mistral in production and is left as the record of how the models were
separated. **Production has since moved to `glm-4.7-flash` with thinking
disabled** (see the model decision at the top of this file), so the deployed
Worker and `main` no longer agree with the table above; the allowlist now holds
the embedder, mistral and glm-4.7-flash.

**Kept from the experiments, both default off and tested to be inert unless
asked for:** `AI_APPEND_NO_THINK` (Qwen3's `/no_think` template token) and
`AI_DISABLE_THINKING` (`chat_template_kwargs.enable_thinking = false`, the
mechanism GLM-4.5 and later honour). The second one is the finding worth
keeping: it turned a model that spent 53 s and 101 neurons on one `insufficient`
into one that answers in 3.3-3.9 s with valid JSON. A reasoning model behind a
switch is fine; the same model without one does not fit a 35 s deadline.

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
2026-09-27 against the deployed Worker and the local stack. **This was measured
with mistral in production and has not been re-run since the move to GLM:**

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

## The finding this pass produced: better retrieval did not make the bot answer

The payment family was the most-asked question in the group — eight attempts
across four phrasings, one success. Investigating why turned up the real
mechanism, and it was not what anyone assumed.

**The distinctive terms of this knowledge base live in the answers, not in the
canonical questions.** Measured in production: `Cluber` appeared in **5 Q&A
answers and 0 canonical questions**, and the venue entry's answer contains
`camp` while its question does not. Retrieval only ever embedded the question,
so those facts were unreachable no matter how good the embedding was. A
lexical (BM25) leg over answer text was built to fix exactly that, and it
worked at its job: `on és el camp?` retrieved the venue answer as its top
lexical hit at BM25 3.72, having missed all five semantic slots before.

**And the bot still got the answer wrong.** Same 26 gold cases, same
generator: **24/26 earned and 88/90 assertions without the leg, 23/26 and
82/85 with it.** All 15 abstention cases still passed in both runs, so the leg
cost no precision on unanswerable questions; it cost one answerable case. For
`On entrenen?` the venue answer arrived at rank 2, the rain policy at rank 1,
and the generator answered *the rain policy* without ever mentioning the venue.

So the leg is **disabled** (`QA_ANSWER_TOP_K=0`) and the code, migration and
62-row FTS projection are all on `main`, with the leg off.
The finding worth keeping is the diagnosis, not the code: **on the questions
that fail, retrieval is not the binding constraint — the generator picking the
wrong one of two plausible candidates is.** The doc had predicted this for
`training_where` ("retrieval returns the rain policy for a question about where
they train, and the model answers it confidently") and it is still true after
the retrieval was fixed.

It is merged rather than parked, and that is deliberate. The leg runs at width 0
so it changes no answer behaviour, prod was already running the same code, and
`migrations/0024_answer_fts.sql` is already applied to production D1 — deleting
it from `main` would leave the repository's schema history disagreeing with the
database it describes. Keeping the projection also keeps the 62 rows current, so
the experiment stays reproducible.

**A correction to earlier reasoning in this file:** the previous session's claim
that the lexical leg "does not pay for the port, the adapter, the fuser, the
projection and the migration" was right about the conclusion and wrong about
the reason. The leg was removed because it had a bug — it joined tokens with
spaces, which FTS5 reads as an implicit AND, so every stopword had to be
present and it returned rows for 2 of 91 questions. The BM25 *recall* figures
were never contaminated by the generator, because recall is computed from the
candidate lists before any generation. What *was* contaminated was the
reranker removal, which was judged on "the model declined" while a reasoning
model was timing out at 53 s. That evidence is void.

Three defects found while building it, two of which no fake could have seen:

- `batched(..., strict=True)` raises unless the count is an exact multiple, so
  every single-row projection failed. Only a test against real SQLite caught
  it; Ruff's `B911` had been trying to warn me.
- `project_qa` reported the lexical failure as `vector_write_failed`, which
  swallowed the cause and cost a deploy cycle to diagnose. The try blocks are
  now split.
- The FTS tokenizer folds diacritics and the query did not, so `llicència` could
  never match the indexed `llicencia`.

And a self-inflicted one worth remembering: **rule 2 of the prompt, added hours
earlier in the same session, silently defeated the entire leg.** It demanded
that an item's question match the user's, which is precisely what a lexical hit
never has. Two correct changes cancelling each other, and only a live probe
caught it. Rule 2 now judges by subject, never by wording.

## Open issues, in the order they will bite

1. **Production is down.** See the top of this file. Nothing else matters until
   Workers AI recovers.
2. **The generator answers the wrong one of two plausible candidates.** This is
   now the measured binding constraint, ahead of retrieval. `training_where`
   has the venue answer at rank 2 and the rain policy at rank 1 and the bot
   describes the rain policy. The fix is a generator or prompt change — for
   instance making the model read each candidate's `Q:` before choosing — and
   it is worth more than any further retrieval work.
3. **The local generation model is broken.** `gemma3:270m` returns
   `{"status": "answered"}` and fails schema validation on every question, so
   any local measurement of answer quality is a measurement of a broken
   generator. `make eval-local` needs Ollama, which is not running, which is
   why the Recall@5 gate is still unmeasured in either direction.
4. **The bot's own meta Q&A pollutes every query.** `Què saps fer?`, `D'on
   treus la informació per respondre?` and `Per què de vegades dus que no ho
   saps?` are official, authority 90, and outrank real club knowledge. For
   `on és el camp?` the semantic top five were the rain policy and three meta
   answers, none mentioning the venue. No retrieval change fixes this; it is a
   knowledge-base design problem, and the most likely reason so many group
   questions abstain.
5. **Training days are 0-for-7** and remain so: asked seven times, answered
   never, because Prebenjamí trains dimarts/dimecres/divendres and Minis
   dilluns/dimarts, and neither is in the knowledge base. The candidates are
   written up in `qa_candidates_review.md`; the Prebenjamí days are
   high-confidence and the Minis days rest on two parents rather than the club.
6. **Five gold failures every model shares.** `equipment_when`,
   `equipment_size`, `training_where`, `medical_expiry`, and
   `gold_abstention_07` fail on mistral, qwen3 and GLM alike. Three models
   moving them by at most one case is the evidence: knowledge and grounding
   defects, not model defects. `training_where` is now understood (issue 2);
   `equipment_size` and `medical_expiry` are not.
7. **The generator is non-deterministic on borderline questions.** The same
   prompt returned `answered` once and `insufficient` three times in four
   repeats of `medical_expiry`. Completeness is currently a rate over single
   runs, which makes any one-case comparison weak evidence — including the
   lexical-leg comparison above.
8. **Retrieval recall is the ceiling.** 0.860 of answerable questions have the
   answer in three candidates, 0.907 in five. The lexical leg was meant to lift
   that and did not move the end-to-end number.
9. **No daily report arrives** until an external scheduler is wired to the
   route.
10. **The Worker cannot host the Logfire SDK.** It exceeds the 128 MB isolate
    limit on import, so production exports nothing and `trace_json` is the only
    durable record of a retrieval decision. A memory-compliant transport is the
    only way to get production traces.



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
