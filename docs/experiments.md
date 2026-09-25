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
