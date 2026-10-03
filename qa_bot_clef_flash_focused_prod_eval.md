# Focused Clef-Flash Production Decision Eval

> **Goal**
>
> Decide whether the QA bot should keep the current production listener decision stack or whether Clef-Flash is good enough to justify a later migration.
>
> This is an **evaluation task**, not a rollout task.
>
> Production Telegram/listener traffic must remain on the current baseline for the entire experiment.

---

## 1. Decision being made

Current production logic:

```text
intent:
    embedding + linear classifier

pairing:
    explicit reply -> pair
    exactly one recent question -> pair
    multiple recent questions -> abstain
```

Candidate alternative:

```text
Clef-Flash
    one System-One request/message
        intent
        relevance(candidate 1)
        relevance(candidate 2)
        ...
```

The question is **not**:

> Is Clef a better generic four-class classifier?

The question is:

> Can Clef preserve the current listener's high precision while correctly resolving useful multi-question cases where the deterministic baseline must abstain?

If not, keep the current system.

---

## 2. Branch and existing implementation

Start from:

```text
branch: feat/system-one-decisions
inspected SHA: 13e132ed0b23d8de28f1cf7f481dbd4e638d93e8
```

The branch already contains:

```text
MessageAssessment
RetroevalCandidate
MessageAssessmentModel
SystemOneTransport
BaselineAssessmentModel
SystemOneAssessmentModel
canonical System-One request builder
strict intent parser
strict relevance parser
candidate collection
model-based pair selection
one-request intent + relevance support
local System-One tests
```

Do not rebuild those abstractions.

---

## 3. Primary eval datasets

The previous 500-case intent proposal is explicitly rejected as the primary gate.

Current repository facts:

```text
data/classifier/test.jsonl
    500 cases
    500 synthetic
    diagnostic only

evals/classifier.yaml
    222 cases
    80 non-synthetic/manual
    142 synthetic

evals/listener.yaml
    60 scenarios
    10 WhatsApp-derived
    50 synthetic
```

The primary decision eval is:

```text
A. 80 manual intent cases
B. 10 existing WhatsApp-derived listener scenarios
C. 50 new hard decision windows
```

Total:

```text
~140 meaningful evaluation units
```

The existing 500 synthetic classifier cases remain optional secondary diagnostics.

They do not determine the final verdict.

---

# 4. Phase 0 — update `AGENTS.md`

Before implementation, extend the current System-One instructions with one narrow production-eval exception.

Add:

```markdown
### Production Clef decision evaluation

When implementing or running the focused Clef-Flash production evaluation:

- Production Telegram and listener traffic remain on `BaselineAssessmentModel`.
- Clef may be called only from the authenticated, side-effect-free internal
  evaluation path.
- Use only `@cf/cloudflare/clef-flash`.
- Reuse the existing `SystemOneAssessmentModel`,
  `build_message_decision_request`, parsers, candidate DTOs, and pairing policy.
- One evaluated listener message produces exactly one Clef request containing
  intent plus every candidate relevance decision.
- Never issue one request per candidate and never perform a second reranking
  pass.
- Clef evaluation has no baseline fallback. A provider or parsing failure is an
  evaluation error.
- Live evaluation requires `ALLOW_CLOUDFLARE_LIVE_TESTS=1`, `BOT_BASE_URL`,
  `INTERNAL_ADMIN_KEY`, and a successful dry-run cost check.
- Live evals never run from CI, startup, cron, deploy hooks, `make all`, or
  normal tests.
- Successful live outputs are cached by exact request + deployed SHA + model.
- The task ends with an evaluation verdict. Enabling Clef for user traffic is a
  separate task.
```

Do not change unrelated repository policy.

---

# 5. Production eval transport

Create:

```text
src/knowledge_bot/infrastructure/cloudflare/system_one.py
```

Implement the existing:

```text
SystemOneTransport
```

with the Workers AI binding.

Use exactly:

```text
@cf/cloudflare/clef-flash
```

The transport performs one:

```text
env.AI.run(...)
```

per `decide()`.

No fallback.

Timeout/provider failures become:

```text
ModelUnavailableError("decision")
```

Reuse the existing strict parser in `application/assessment.py`.

---

# 6. Keep runtime traffic baseline

This is mandatory.

Normal Cloudflare composition must still use:

```python
assessment = BaselineAssessmentModel(classifier)
```

for:

```text
ListenerIngestor
BackgroundIndexer
MessagePairingService
Telegram traffic
```

Add a **separate eval-only assessment**:

```python
decision_evaluator = SystemOneAssessmentModel(
    transport=WorkersAISystemOneTransport(...),
    model="@cf/cloudflare/clef-flash",
    include_relevance=True,
    fallback=None,
    ...
)
```

This object is reachable only from the internal eval route.

Do not select it from `DECISION_BACKEND` in production.

---

# 7. Internal eval endpoint

Add:

```text
POST /internal/eval/decision
```

It must be:

```text
internal-key protected
rate limited through existing eval admission
budget protected through existing eval budget guard
side-effect free
```

It must never:

```text
persist messages
persist pairs
write Vectorize
send Telegram
write feedback
change bot state
```

Request:

```json
{
  "backend": "baseline | clef-flash",
  "cases": [
    {
      "case_id": "...",
      "text": "...",
      "candidates": [
        {
          "candidate_id": "...",
          "question_message_id": "...",
          "question": "...",
          "relation": "explicit_reply | temporal_window"
        }
      ]
    }
  ]
}
```

Limits:

```text
max 5 cases/request
max 5 candidates/case
```

For each case return:

```text
intent probabilities
best label
best score
margin
prefiltered flag
relevance probabilities
duration
or explicit model error
```

Do not substitute baseline output when Clef fails.

---

# 8. Suite A — 80 manual intent cases

Primary intent set:

```text
the 80 non-synthetic cases from evals/classifier.yaml
```

Do not use:

```text
data/classifier/test.jsonl
```

for the primary decision.

Evaluate:

```text
production baseline
vs
Clef-Flash
```

on exactly the same cases.

Report:

```text
accuracy
macro F1
question recall
knowledge_update precision
correction precision
chitchat precision
confident precision
coverage
confusion matrix
error rate
latency p50/p95
```

Use current confidence policy:

```text
best score >= 0.60
margin >= 0.15
```

Do not tune intent thresholds.

---

# 9. Intent acceptance rule

Clef passes intent safety if:

```text
question recall:
    no worse than baseline by > 0.02

knowledge_update precision:
    no worse than baseline by > 0.02

correction precision:
    no worse than baseline by > 0.02

confident precision:
    no worse than baseline by > 0.01

coverage:
    no worse than baseline by > 0.05 absolute

model/parser error rate:
    < 1%
```

Macro F1 is reported, but safety metrics dominate.

Do not reject or accept Clef based on the synthetic 500-case F1.

---

# 10. Suite B — existing real WhatsApp listener cases

Use only the 10 scenarios in:

```text
evals/listener.yaml
```

with:

```text
synthetic: false
```

These are regression cases.

Replay them using the real application listener/pairing code.

Run twice:

```text
baseline
Clef-Flash
```

Metrics:

```text
question detection
expected answer-like messages
pair precision
pair recall
wrong pairs
missed pairs
unexpected indexing
```

Required:

```text
zero wrong pairs in the 10 real scenarios
```

Because this set is small, report the cases individually, not only aggregate percentages.

---

# 11. Suite C — 50 hard decision windows

Create:

```text
evals/decision_windows.yaml
```

This is the main differentiating eval.

Exactly 50 scenarios:

| category | total |
|---|---:|
| one open question + correct answer | 5 |
| 2–5 open questions + exactly one correct | 15 |
| 2–5 open questions + no correct candidate | 10 |
| two genuinely plausible candidates | 5 |
| explicit reply + genuine answer | 5 |
| explicit reply + unrelated/non-answer | 5 |
| open questions + chitchat/noise | 5 |
| **total** | **50** |

Do not change these counts.

---

# 12. Build the 50 windows from repository knowledge

Use:

```text
data/seed/qa.json
evals/retrieval_synthetic.yaml
current embedding model
```

Do not use a generative model to invent arbitrary facts.

## Correct pairs

A canonical:

```text
question Q_i
answer A_i
```

is the positive relation.

Synthetic query variants mapped to the same `source_anchor` may replace `Q_i`.

## Hard negatives

For every positive question:

1. embed the query;
2. rank canonical questions;
3. remove:
   - same anchor;
   - `also_accepts`;
4. take the nearest wrong questions.

Hard negatives must therefore be semantically close.

Do not fill windows with random unrelated questions.

---

# 13. Window construction rules

## 13.1 One open question + correct answer — 5

```text
candidate count = 1
candidate = correct
```

Expected:

```text
pair
```

This is mainly a safety regression.

---

## 13.2 Multi-question, one correct — 15

```text
candidate count = 2..5
exactly one positive
rest hard negatives
```

Expected:

```text
select the positive candidate
```

This is the highest-value category.

The deterministic baseline normally abstains when multiple temporal candidates are open.

Clef earns its complexity here.

---

## 13.3 No correct candidate — 10

Use:

```text
answer A_i
2..5 hard-negative questions
Q_i absent
```

Expected:

```text
no pair
```

This directly measures false-positive pairing risk.

---

## 13.4 Two plausible candidates — 5

Choose cases where the same short answer could reasonably fit two candidate questions.

Example shape:

```text
answer:
    "A les sis."

candidates:
    "A quina hora entrenem?"
    "A quina hora és el partit?"
```

Expected:

```text
abstain
```

The model must not invent confidence when context is insufficient.

These 5 cases require explicit human review.

---

## 13.5 Explicit reply + genuine answer — 5

The message explicitly replies to the correct question and materially answers it.

Expected:

```text
pair with explicit parent
```

---

## 13.6 Explicit reply + unrelated/non-answer — 5

The message is an explicit reply but does not answer the parent.

Examples:

```text
"gràcies"
"ho miro després"
"no ho sé"
"parlem demà"
```

Expected:

```text
no pair
```

This is an area where Clef can be safer than the current deterministic explicit-reply rule.

---

## 13.7 Open questions + chitchat/noise — 5

There are open candidate questions, but the current message is noise/social conversation.

Expected:

```text
no pair
```

---

# 14. Human review of the 50 cases

After deterministic generation:

```text
STOP before running Clef.
```

Review every case manually once.

For each case confirm:

```text
expected intent is defensible
positive candidate really is answered
hard negatives really are negative
ambiguous cases really are ambiguous
no hidden answer exists in a supposed no-correct window
```

Commit the reviewed dataset.

After review:

```text
do not edit it based on Clef outputs
```

---

# 15. Fixed calibration/test split

Do not randomly split after seeing model results.

Use exactly:

```text
15 calibration cases
35 held-out test cases
```

Allocation:

| category | calibration | test |
|---|---:|---:|
| single correct | 2 | 3 |
| multi one-correct | 5 | 10 |
| no correct | 3 | 7 |
| ambiguous two-plausible | 1 | 4 |
| explicit genuine | 1 | 4 |
| explicit unrelated | 1 | 4 |
| chitchat/noise | 2 | 3 |
| **total** | **15** | **35** |

Assign cases deterministically while building the dataset.

Store:

```text
split: calibration | test
```

inside the YAML.

Do not move cases between splits after seeing outputs.

---

# 16. Primary Clef run uses current thresholds

First evaluate the 50 cases with the current branch values:

```text
relevance threshold = 0.80
relevance margin = 0.15
```

This is the **primary result**.

Do not tune before recording it.

---

# 17. Calibration

After the primary run, use only the 15 calibration cases.

No new Clef calls.

Reuse cached probabilities.

Grid:

```python
thresholds = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
margins = [0.00, 0.05, 0.10, 0.15, 0.20]
```

Select:

1. reject any configuration with a wrong pair;
2. maximize correctly recovered pairs;
3. tie -> maximize correct abstentions;
4. tie -> higher threshold;
5. tie -> higher margin.

Freeze the result.

Then apply it once to the 35 held-out cases.

Do not recalibrate again.

---

# 18. Main decision metrics

For the 35 held-out windows report:

```text
pair precision
pair recall
wrong-pair count

multi-question correct selection:
    correct / 10

no-correct abstention:
    correct / 7

ambiguous abstention:
    correct / 4

explicit genuine:
    correct / 4

explicit unrelated rejection:
    correct / 4

chitchat rejection:
    correct / 3
```

Also report the same metrics for the baseline.

---

# 19. The key comparison

The most important number is:

```text
multi-question correct selection
```

because:

```text
baseline:
    >1 temporal candidate
    -> abstain

Clef:
    can potentially select the correct one
```

If Clef does not recover a meaningful number of these cases, do not replace the baseline.

---

# 20. Decision gates

Clef is worth considering only if **all safety gates** pass:

```text
intent safety:
    passes Section 9

10 real WhatsApp scenarios:
    0 wrong pairs

35 hard held-out windows:
    pair precision >= 0.98
    wrong pairs <= 1
    no-correct abstention >= 6/7
    ambiguous abstention >= 3/4
    explicit unrelated rejection >= 3/4
    chitchat rejection == 3/3

runtime:
    model/parser error rate < 1%
    one System-One request per assessed message
```

And at least one **value gate** must pass:

```text
multi-question correct selection >= 7/10
```

or:

```text
held-out pair recall improves over baseline by >= 0.15 absolute
```

If safety passes but neither value gate passes:

```text
KEEP_CURRENT
```

because Clef adds cost and complexity without enough benefit.

---

# 21. Latency

Measure real production Worker latency for Clef decisions.

Report:

```text
0 candidates
1 candidate
2–5 candidates
```

with:

```text
p50
p95
```

Suggested practical gates:

```text
0 candidate p95 <= 300 ms
2–5 candidates p95 <= 500 ms
```

Latency does not need to beat the linear classifier.

It only needs to be operationally negligible relative to the rest of the background listener flow.

---

# 22. Cost

Before any live run, perform a dry run.

Calculate from the exact serialized requests:

```text
number of Clef calls
serialized characters
estimated input tokens
estimated neurons
```

Use a conservative token estimate.

Set a hard full-experiment cap:

```text
1500 neurons
```

The focused experiment should comfortably fit below this.

If the estimate exceeds 1500:

```text
STOP
```

Do not add `--force`.

---

# 23. Result caching

Cache successful Clef outputs in:

```text
.eval-cache/clef-flash-production.jsonl
```

Key:

```text
deployed SHA
+
model id
+
SHA256(canonical state + questions)
```

Add:

```text
.eval-cache/
```

to `.gitignore`.

Never pay twice for an unchanged case.

Failures are recorded in the report but are not cached as successful outputs.

---

# 24. Production eval runner

Create:

```text
evals/decision_production.py
```

It owns:

```text
loading the three suites
calling /internal/eval/decision
cache lookup/write
dry-run cost calculation
metric calculation
calibration grid
report generation
```

It must not reproduce application pairing rules.

For the listener/window suites, use the existing:

```text
MessagePairingService.select_pair()
SystemOneAssessmentModel semantics
```

where practical.

Keep evaluation policy in one place.

---

# 25. Make targets

Add:

```make
eval-clef-production-dry:
	@test -n "$(BOT_BASE_URL)" || \
		(echo "BOT_BASE_URL is required" >&2; exit 2)
	uv run python -m evals.decision_production \
		--dry-run \
		--base-url "$(BOT_BASE_URL)"

eval-clef-production:
	@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = 1 || \
		(echo "ALLOW_CLOUDFLARE_LIVE_TESTS=1 is required" >&2; exit 2)
	@test -n "$(BOT_BASE_URL)" || \
		(echo "BOT_BASE_URL is required" >&2; exit 2)
	@test -n "$(INTERNAL_ADMIN_KEY)" || \
		(echo "INTERNAL_ADMIN_KEY is required" >&2; exit 2)
	uv run python -m evals.decision_production \
		--base-url "$(BOT_BASE_URL)"
```

Do not add either target to CI or `make all`.

---

# 26. Implementation tests

Before deployment:

```text
WorkersAISystemOneTransport fake tests
/internal/eval/decision auth tests
side-effect-free endpoint tests
one-call-per-case tests
5-candidate -> 6 System-One questions
Clef failure -> eval error, no fallback
Cloudflare runtime assessment remains baseline
```

Run:

```bash
make format
make lint
make test
make test-integration
uv run python -m evals.run offline
make smoke
```

---

# 27. Live execution order

Use exactly this order.

## 1. Build and human-review the 50 decision windows

Do not run Clef yet.

## 2. Deploy the eval-only endpoint

Production listener stays baseline.

## 3. Smoke

Run:

```text
1 intent-only Clef case
1 one-candidate case
1 five-candidate case
```

Verify:

```text
valid parser output
one model call/case
2 decisions for one-candidate
6 decisions for five-candidate
```

## 4. Dry run

```bash
make eval-clef-production-dry
```

Confirm:

```text
estimated neurons <= 1500
```

## 5. Check production budget

Use:

```text
GET /internal/budget
```

Do not run when the daily evaluation budget is already nearly spent.

## 6. Run once

```bash
ALLOW_CLOUDFLARE_LIVE_TESTS=1 \
BOT_BASE_URL=... \
INTERNAL_ADMIN_KEY=... \
make eval-clef-production
```

No automatic retry.

## 7. Generate report

Then stop.

---

# 28. Optional diagnostics after the decision result

Only if useful for understanding failures:

```text
142 synthetic cases from evals/classifier.yaml
500 synthetic cases from data/classifier/test.jsonl
50 synthetic listener cases from evals/listener.yaml
```

These results are appendix material only.

They do not change the primary verdict.

Do not spend neurons on them automatically.

---

# 29. Final report

Generate:

```text
reports/clef-flash-focused-eval.md
```

Required sections:

```text
experiment SHA
production runtime safety confirmation
Clef configuration
total Clef calls
estimated neurons
cache hits

80 manual intent results
10 real WhatsApp cases individually

50 hard-window dataset description
15 calibration cases
35 held-out cases

baseline vs Clef:
    pair precision
    pair recall
    multi-question recovery
    no-correct abstention
    ambiguous abstention
    explicit-reply safety

fixed 0.80/0.15 result
calibrated result
latency
provider/parser errors
one-pass invariant

final verdict
```

---

# 30. Final verdict

Use exactly one:

```text
KEEP_CURRENT
```

or:

```text
CLEF_IS_WORTH_A_PRODUCTION_ROLLOUT_PLAN
```

The second verdict does **not** enable Clef.

It means:

```text
the next task may design the rollout
```

If Clef does not materially recover multi-question cases while preserving current precision:

```text
KEEP_CURRENT
```

---

# 31. Explicit non-goals

```text
NO production Clef traffic
NO shadow listener traffic
NO Telegram A/B
NO fine-tuning
NO alternative models
NO new generic AI abstraction
NO model request per candidate
NO second model pass
NO threshold tuning on held-out test
NO use of 500 synthetic classifier cases as the primary gate
NO automatic repeat of live eval
NO runtime setting changes based on the result in this task
```

---

# 32. Definition of done

This task is done when we can answer, with production-model evidence:

```text
1. Does Clef preserve intent safety?
2. Does Clef preserve the 10 real listener regressions?
3. Can Clef correctly resolve multi-question windows?
4. Does Clef abstain when no safe pair exists?
5. Does it do so in one request/message?
6. Is latency acceptable?
7. Is the extra neuron cost justified by the added recall?
```

and the report concludes:

```text
KEEP_CURRENT
```

or:

```text
CLEF_IS_WORTH_A_PRODUCTION_ROLLOUT_PLAN
```
