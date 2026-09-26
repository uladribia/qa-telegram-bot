# Experiments

Measured experiments behind design decisions, with the numbers that justified
them and the state of each. Everything here was measured locally (Ollama
`embeddinggemma`, SQLite FTS5) unless marked as a live production run; live runs
state their neuron cost.

## Answer selection: always ground in the model

**Question.** Should the bot echo the best-matching Q&A verbatim, compose an
answer with the model, or learn a selector?

**Measured on the two gold sets** (76 human answer cases, 308 abstention cases,
47 of which have a known expected anchor, 500 retrieval queries):

| behaviour | unknown questions answered | model can decline | neurons per answer |
|---|---:|---|---:|
| verbatim echo (previous rule) | **98.7%** | no | 0.9 |
| always the model, top-5 Q&A | 100% sent to the model | yes | ~102 |

The verbatim echo was the problem, not the baseline: it cannot decline, so it
answered 304 of 308 *unknown* questions with the nearest document's answer. The
model returns `insufficient` for almost every unknown question, so sending the
question to the model is the **safer** behaviour and costs about 100 neurons per
answered question.

**Evidence quality.** When a question is answerable, the expected anchor is
inside the top-5 evidence for 97% of the 500 retrieval queries and 27 of the 47
labelled answer cases.

**Cost.** Prompt with 5 Q&A items is ~4,900 characters, so about 102 neurons per
answered question at the estimator's 0.020 neurons/char, plus 0.9 for the query
embedding. With 3 evidence items it is ~76. The 10,000-neuron daily budget
therefore supports roughly 95 questions/day at 5 items and 130 at 3.

**Decision.** Always ground in the model. `ANSWER_SIMILARITY_FLOOR` is the
single knob: Q&A above the floor are sent to the generator, and a question with
nothing above it abstains. The local floor curve is flat between 0.30 and 0.70,
so the knob barely affects safety there.

**Production calibration.** The local curve did not transfer: short, real
questions score lower than the generated paraphrases the curve was measured on.
Tuned live on four known-answerable and three unknown questions:

| floor | known answered | unknown answered |
|---|---:|---:|
| 0.70 | 1/4 | 0/3 |
| **0.45** | **2-3/4** | **0/3** |

0.45 is the deployed value: it restores coverage on short real questions while
the unknown questions still abstain. Two responses in that run came back
``unavailable`` - the transient model failure we already know about, now visible
on a larger share of traffic because every question reaches the model. It is the
main operational cost of this design and the reason to watch it after release.

## Learned answer-relevance head: measured and rejected

**Question.** Can a small trained head replace the fixed similarity thresholds?

**What was built.** A logistic-regression head over 17 features (ranks, RRF,
relative and absolute cosine, lexical rank, text overlap), trained on 4,470
candidate pairs mined from real hybrid retrievals, split by query with the 33
human gold queries held out. Runtime cost: zero model calls, plain Python.

**Result across three iterations**, on the deciding evaluation (76 human cases):

| iteration | features | mode accuracy | abstention rate (309) | right doc (27 answerable) |
|---|---|---:|---:|---:|
| 1 | 17, text-heavy | 0.171 | 0.994 | 7/27 |
| 2 | + absolute cosine, short-question training | 0.066 | 0.994 | - |
| 3 | 10, PCA embedding coordinates + relative policy | 0.303 | 0.774 | 7/27 |
| baseline | fixed thresholds | **0.737** | 0.007 | **17/27** |

**Why it failed.** The head's fitted weights concentrated on the surface
features that punish a terse real question for not sharing wording with the
canonical question, and when those were removed it fell back onto absolute
cosine - the one quantity that does not transfer between embedding providers.
The head learned to be safe (0.77-0.99 abstention) without being useful.

**Kept as evidence.** The dataset, the trainer, the local comparison and the
PCA projection remain on the branch `feat/relevance-and-pairing-heads` for
reference. They are not on `main` and are not used at runtime.

**What is actually missing.** A labelled answerability signal. Retrieval only
returns the expected anchor for 27 of the 47 answer cases, and nothing in the
corpus labels "answerable" versus "plausible-looking but unanswerable". The
honest next step for a learned selector is to mine answerable pairs from the
real corpus (retrieval hit plus human gold anchor) and unanswerable ones from
the abstention set, and train on those - not on generated paraphrases.

## Retrieval: the real ceiling

Question-focused embeddings plus BM25/RRF reach Recall@1 0.964, Recall@3 0.984,
Recall@5 0.986 and MRR 0.973 locally, against a question+answer embedding
baseline of 0.308 / 0.540 / 0.690 and 0.455. On the 47 labelled *human* answer
questions the gold anchor is retrieved for 27: short, conversational questions
remain the dominant retrieval miss, and no answer policy can repair that. The
next retrieval effort should target terse questions.

## Threshold transfer between providers

A fixed absolute cosine cut is meaningless across deployments: production
(Workers AI `embeddinggemma-300m`) and local (Ollama `embeddinggemma`) put
cosine on different scales, which is why the live answer suite failed locally
while passing local metrics. The shipped policy avoids the problem by making
every decision relative to the request, with one low absolute floor that only
separates "has any support" from "no support".

## Pairing: learned head measured, not shipped

The deterministic listener pairs a confident update or correction with exactly
one plausible unresolved question inside a 5-minute window (max 5 candidates),
never calls a model, and reaches pair precision 1.0. Explicit replies pair
immediately.

A learned pair scorer was built and measured: 13 features (embedding cosine
between the question and the answer, that cosine's margin over competing
candidates, token and character overlap, age, candidate count, a correction cue,
and classifier confidences), trained on pairs where the positive is the
question and the answer that responds to it and the hard negatives are the same
question with a shifted day or time, plus chitchat and unrelated questions.

**Held-out synthetic test split:** pair precision **0.975** at the calibrated
operating point (`tau=0.90`, `margin=0.0`), PR-AUC 0.978, recall 0.982 at the
default cut. The gate (precision >= 0.95) passes.

**Realistic scenarios** (`uv run python -m evals.pairing`, 24 scenarios with
short human questions and plain club updates):

| metric | deterministic rule | pairing head |
|---|---:|---:|
| pairs made | 7 | 4 |
| precision | 1.0000 | 1.0000 |
| recall | **0.4375** | 0.2500 |
| scenario accuracy | 0.6250 | 0.5000 |

The head does not transfer. Its training data is templated
question/answer pairs that share explicit detail words, while real questions
and updates are short and share almost nothing lexically, so the head scores
real candidates below its floor and pairs less than the rule it was meant to
improve. Precision is unchanged; recall is strictly worse.

**Decision: not enabled.** The head ships disabled
(`PAIRING_HEAD_ENABLED=false`). What is kept and ready: the persisted question
embeddings (`intent_embedding`, migration 0022) that make the feature free at
request time, the feature computation, the policy, the trainer, and the
scenario eval. The missing ingredient is the same one the answer head lacked:
real labelled pairs. The next step is to harvest them from the
`message_pair_candidates` audit rows the listener has been recording, once a
group has enough real traffic to label them.

## Generation model: reasoning, and the cost of it (live)

**Question.** The deployed generator, `@cf/zai-org/glm-4.7-flash`, made users
report "the bot does not answer". Was the model at fault?

**Yes, and it was measurable only after the pipeline was instrumented.** The
webhook handler logged 37.2 s wall for a 46-character question, and 466 ms of
CPU — the time was a wait, not compute. With one log line per Workers AI call
(`workers_ai_call_completed` / `workers_ai_call_failed`, carrying `operation`,
`model`, `characters`, `duration_ms`, `error`) the split was immediate:

```text
operation: embedding   characters: 29     duration_ms: 498
operation: generation  characters: 2764  duration_ms: 35000  error: TimeoutError
```

Everything owned by this codebase cost half a second. One call to the model
consumed the remaining 35 s and never returned. Called directly through the
Cloudflare API with the exact production payload, the same model took **53.0 s,
2653 completion tokens, 10 551 characters of `reasoning_content`, 101
neurons** to return `insufficient`. It is a reasoning model: it emits a chain of
thought and only then the answer, and the deliberation is unbounded.

**Screen across models, on the real evidence.** Four candidates, same payload,
`max_tokens` 1024, judged on whether they invented facts when the evidence did
not answer the question:

| Model | Wall | Neurons | On evidence that does not answer |
|---|---:|---:|---|
| `@cf/mistralai/mistral-small-3.1-24b-instruct` | 2.5 s | 27 | correct `insufficient` |
| `@cf/meta/llama-3.1-8b-instruct-fp8` | 3.4 s | 13 | **hallucinated** a payment method |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | 1.5 s | 37 | **hallucinated** |
| `@cf/meta/llama-3.2-3b-instruct` | fast | 9 | **hallucinated** |

Mistral shipped: 21x faster, a quarter of the neurons, and the only candidate
that still refused. The three Llamas invented a payment channel that appears in
no document, which is the failure this bot exists to prevent. **Model size is
not a proxy for grounding; the small models bought speed with invented facts.**

Two caps shipped with it. `AI_GENERATION_MAX_TOKENS=1024`, because uncapped the
model outlasted the deadline; answerable questions need 675-851 completion
tokens, so 1024 keeps real answers and turns the pathological ones into a fast
abstention. And no `response_format` — the system prompt already fixes the JSON
shape and the output is validated locally, so the structured-output mode only
added a latency path. (It was suspected as the cause first and was **not**; it
stayed out on the grounds that it bought nothing.)

**The axis that was missed.** Mistral was screened on grounding and not on
completeness, and completeness is what fails: on the live answer suite it
declined 22 of 43 answerable questions. Any future model comparison needs both
numbers, and a completeness number has to be a rate — the same prompt on the
same five documents returned `answered` in five offline reproductions and
`abstention` in production. The model is non-deterministic on borderline
questions, so the suite score is noisy in both directions.

## Cross-encoder reranking: measured, harmful, removed

**Question.** Would a reranker fix the near-miss evidence that reaches the
generator?

`@cf/baai/bge-reranker-base` is cheap and fast in isolation — one batched call
scores the whole candidate list, 0.49 s and 0.33 neurons for 1182 input tokens,
one subrequest, and it separates sharply (on-topic 0.0149 against 0.0030 for the
runner-up and ~1e-4 for the rest). It made retrieval **worse**. Reordering by
cross-encoder score promotes a high-relevance, low-cosine item and pushes a
high-cosine item out of the top-5, thinning the generation prompt from ~4200 to
1047-1400 characters. A model instructed to return `insufficient` when the
evidence is insufficient does exactly that when handed one thin document. Net
effect: 22 of 43 answerable questions abstaining.

The score is also useless as a replacement floor. Across all 76 eval questions
the top-1 rerank score spans 0.9997 down to 0.0000 for answerable questions and
0.3175 down to 0.0000 for unanswerable ones — the same distribution twice:

| Rerank floor | Answerable answered | Unanswerable wrongly answered |
|---:|---:|---:|
| 0.0005 | 38/43 | 13/21 |
| 0.005 | 34/43 | 7/21 |
| 0.05 | 28/43 | 3/21 |

Every threshold loses more answers than it saves. Removed. BM25/FTS5 stays: the
only measured retrieval numbers here are hybrid MRR 0.973 / Recall@5 0.986
against a semantic-only baseline of 0.455, and the corpus turns on rare proper
nouns (*Pau Negre*, *Cluber*, *Minis*, *FCB*) where dense embeddings are weakest.

## Answer floor: 0.45 to 0.35, and what it is for

**Question.** The floor discards retrieved Q&A below a cosine cut. What should
it be, and is it doing precision work?

It is not a precision device. At 0.45, **ten of the twenty unanswerable eval
questions already cleared it** (top-1 cosine up to 0.599), while the answerable
questions bottom out at 0.357. It was a *thickness* knob all along, set to a
value that quietly starved the generator: 6 of the 43 answerable questions were
cut to two or three items.

`0.35` is the lowest top-1 cosine any of the 43 answerable questions has, and
at 0.35 the top-5 gives **43/43 of them a full five-item evidence set**. It
costs no precision, because the floor was never providing any. Lowering it moved
production prompts from 1047-1400 to 2520-4473 characters.

It did not change the verdicts, which is the honest result: the model declines
on borderline evidence regardless of how much of it it is given. The floor stays
at 0.35 because it is the correct calibration, not because it fixed anything.

## Delivery: the webhook answered Telegram after 37 s

**Question.** Why did the group see "no info available" *and* sometimes nothing
at all?

Two failures with one cause. The whole AI pipeline ran inside the webhook
request, so a 37 s handler overran Telegram's read timeout; `getWebhookInfo`
showed `pending_update_count: 1` and a retry minutes later. The answer still
arrived, because it is delivered by an independent `sendMessage` call — which is
why the symptom looked like the bot answering and not answering at the same
time. The Worker now acknowledges `{"status": "accepted"}` immediately and
processes in a `waitUntil` task, which `workers.asgi` already supports.

## Cron Triggers do not run on the Workers Free plan

**Question.** Why did `daily_report_state` stay empty with the schedule
registered and the route proven to work?

`GET /workers/scripts/{name}/schedules` returned `0 19 * * *` and the route
returned `{"status": "sent"}` when called by hand, so the failure was invisible:
a Cron Trigger on the Free plan gets **10 ms of CPU** (30 s on paid) against a
**3451 ms** `Worker Startup Time`. The interpreter cannot start, so
`Default.scheduled` never reaches its first statement — which is why adding
`scheduled_started` / `scheduled_finished` / `scheduled_failed` logging changed
nothing. The tail confirmed it: zero events at 19:00 UTC across two consecutive
windows, with the tail connected.

The report is now delivered by an external scheduler calling
`POST /internal/jobs/daily-report`, which the code already supported. The
instrumentation stayed, because "the job ran and skipped" and "the job never
started" are otherwise indistinguishable.
