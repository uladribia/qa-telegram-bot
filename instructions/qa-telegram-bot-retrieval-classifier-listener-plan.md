# QA Telegram Bot — Retrieval, Classifier and Listener Improvement Plan

**Goal:** improve precision without increasing Cloudflare AI usage or complicating the architecture.

**Do not change:** GLM-4.7-Flash, EmbeddingGemma, Vectorize, D1, SQLite, NumPy, Ollama, answer modes, correction workflow.

Implement in **one pass** on one branch.

---

# 1. Final target

Implement exactly these changes:

1. **Question-focused embeddings**
   - Q&A vector text = canonical question only.
   - Paired message evidence vector text = `context_question` only.
   - Standalone factual update vector text = message text.

2. **Hybrid retrieval**
   - semantic search from existing vector store;
   - BM25/FTS5 lexical search;
   - Reciprocal Rank Fusion (RRF);
   - same-canonical local override;
   - authority only as final tie-break.

3. **Classifier**
   - replace hand-written prototype classifier with one multinomial logistic-regression head;
   - train locally on **Ollama `embeddinggemma`** embeddings;
   - production uses the same exported linear head on the existing embedding vector;
   - no sklearn dependency at runtime.

4. **Listener**
   - classify each non-trivial background message once;
   - index only high-confidence `knowledge_update` / `correction`;
   - questions become pending question candidates;
   - explicit replies pair deterministically;
   - otherwise pair only with one unambiguous recent question;
   - ambiguous/chitchat is stored but not indexed as factual evidence.

5. **Evaluation**
   - expand classifier train split to exactly **500 cases**;
   - expand classifier test split to exactly **500 disjoint cases**;
   - keep all existing human-written cases;
   - generate the rest synthetically;
   - train locally;
   - evaluate classifier, listener and retrieval;
   - write a Markdown results report with before/after metrics.

No Cloudflare training or eval runs.

---

# 2. Branch

```bash
git switch main
git pull --ff-only
git switch -c improve/retrieval-classifier-listener
```

Do not create additional branches.

---

# 3. Dataset handling

The working branch may already contain JSON classifier train/test files.

## 3.1 Existing JSON splits

First locate the existing classifier JSON/JSONL training and testing files.

Rules:

- **do not rename them**;
- **do not delete or rewrite existing human-labelled examples**;
- treat existing examples as fixed gold data;
- preserve train/test separation;
- deduplicate normalized text across both splits.

If no JSON train/test split actually exists in the working tree, create:

```text
data/classifier/train.jsonl
data/classifier/test.jsonl
```

with schema:

```json
{"text": "Demà entrenen?", "label": "question", "source": "human"}
```

Allowed labels:

```text
question
knowledge_update
correction
chitchat
```

The committed `evals/classifier.yaml` cases are mandatory seed/regression cases. Import or mirror them without removing the original YAML eval.

---

# 4. Expand classifier data to exactly 500 + 500

Create:

```text
scripts/generate_classifier_dataset.py
```

It must produce deterministic synthetic examples with a fixed seed.

Final counts:

```text
train = exactly 500
test  = exactly 500
```

The two sets must be disjoint after:

```python
normalize = " ".join(text.casefold().split())
```

## 4.1 Preserve existing examples first

Order:

```text
existing human train
existing human test
existing evals/classifier.yaml cases
synthetic additions
```

Never move a human test example into training.

If a YAML case duplicates existing JSON, keep one copy only.

---

## 4.2 Synthetic class balance

Target approximately:

```text
question          150 / split
knowledge_update  125 / split
correction        100 / split
chitchat           125 / split
```

Small deviations are allowed only to preserve existing human examples.

Each split must total exactly 500.

---

## 4.3 Synthetic coverage

Generate realistic Catalan-first school/team chat language, with some Spanish and mixed Catalan/Spanish.

Mandatory hard contrasts:

```text
"Demà a les sis."                  -> knowledge_update
"Demà a les sis?"                  -> question

"Finalment és diumenge."           -> knowledge_update
"No, finalment és diumenge."       -> correction

"Perfecte, diumenge doncs."        -> chitchat
"Diumenge?"                         -> question

"Crec que era a les sis."          -> chitchat or low-confidence update,
                                      choose one label consistently

"Han canviat l'hora."              -> knowledge_update
"T'has equivocat, han canviat l'hora." -> correction
```

Include variation in:

- punctuation;
- missing accents;
- typos;
- short messages;
- long messages;
- emoji;
- dates/times;
- categories/team names;
- transport;
- equipment;
- medical certificate;
- training;
- matches;
- payments;
- cancellations;
- reminders;
- corrections;
- acknowledgements.

Do not generate 500 template clones.

Use at least 50 linguistic templates and varied slots.

---

# 5. Train one local classifier head

Create:

```text
scripts/train_classifier.py
```

Training runtime:

```text
Ollama model: embeddinggemma
```

Workflow:

```text
train texts
→ Ollama embeddings
→ multinomial logistic regression
→ validation on fixed 500-case test set
→ export coefficients
```

Use `scikit-learn` as a **dev/training dependency only**.

Do not import sklearn from `src/`.

Export:

```text
data/classifier/model.json
```

Schema:

```json
{
  "embedding_model": "embeddinggemma",
  "labels": [
    "question",
    "knowledge_update",
    "correction",
    "chitchat"
  ],
  "coef": [[...], [...], [...], [...]],
  "intercept": [...],
  "train_sha256": "...",
  "created_from_cases": 500
}
```

Do not export pickle/joblib.

---

# 6. Runtime linear classifier

Replace prototype-based classification with:

```text
message embedding
→ linear logits
→ softmax probabilities
```

No prototype embeddings.

No sklearn runtime dependency.

Implement matrix operations with plain Python or NumPy already available in the project.

Return:

```text
label probabilities
top label
top probability
top1 - top2 margin
embedding
```

Keep the existing trivial deterministic prefilter for:

- empty messages;
- emoji-only;
- exact acknowledgements such as `ok`, `gràcies`, `perfecte`.

---

## 6.1 Confidence policy

Start with:

```text
top_probability >= 0.60
AND
top1_minus_top2 >= 0.15
```

Otherwise classify as operationally **ambiguous**.

Do not add `ambiguous` as a trained class.

Behavior:

```text
question          high confidence -> pending question
knowledge_update  high confidence -> factual evidence
correction        high confidence -> factual evidence
chitchat          high confidence -> store only
ambiguous                         -> store only
```

Do not tune these thresholds against the test set.

If tuning is required, split a validation subset from the 500 training examples only.

---

# 7. Question-focused embeddings

Change projection behavior.

## Q&A

Current:

```python
embed(question + "\n" + answer)
```

Replace with:

```python
embed(question)
```

Keep answer in metadata.

## Paired message evidence

If:

```text
context_question is not None
```

embed:

```text
context_question
```

Keep answer/message text in metadata.

## Standalone factual update

Embed:

```text
message.text
```

No extra embedding calls.

---

# 8. Add BM25 / FTS5

Do not add Elasticsearch, OpenSearch, Whoosh or another service.

Use SQLite/D1 FTS5.

Create one derived lexical index table, for example:

```sql
CREATE VIRTUAL TABLE search_fts USING fts5(
    vector_id UNINDEXED,
    kind UNINDEXED,
    scope_key UNINDEXED,
    canonical_key UNINDEXED,
    question_text
);
```

The FTS index is a **derived projection**, never source of truth.

Index:

```text
Q&A:
    question_text = canonical question

paired message:
    question_text = context_question

standalone update:
    question_text = message text
```

Projection lifecycle must stay synchronized with vector projection:

```text
project -> update vector + FTS
delete  -> delete vector + FTS
rebuild -> rebuild vector + FTS from SQL truth
repair  -> repair both
```

---

# 9. Hybrid retrieval with RRF

For each user question:

```text
1 embedding
```

Then retrieve:

```text
semantic top 10
BM25 top 10
```

for each relevant scope/kind.

Do not run another model.

Fuse with Reciprocal Rank Fusion:

```python
rrf_score = 1 / (60 + semantic_rank) + 1 / (60 + bm25_rank)
```

A result appearing in only one list gets only that term.

Do not combine raw cosine and BM25 scores.

---

## 9.1 Scope and override rules

For Q&A:

```text
global semantic + BM25
local semantic + BM25
```

Then:

1. local candidate suppresses global only when same `canonical_key`;
2. fuse/rank remaining candidates by:
   - RRF descending;
   - authority descending;
   - semantic similarity descending as final deterministic tie-break;
3. return top `qa_top_k`.

For message evidence:

- local space only for normal group queries;
- never expose another space's evidence.

---

# 10. Conservative listener enhancement

Remove LLM-based temporal pairing from the normal listener path.

Do not call `gemma3:270m` or GLM just to pair ordinary messages.

Use deterministic pairing.

## 10.1 Explicit reply

If a high-confidence answer-like message is an explicit Telegram reply to a high-confidence question:

```text
pair immediately
```

This is the strongest signal.

---

## 10.2 Temporal pairing

Maintain unresolved recent high-confidence questions per conversation.

Window:

```text
max age = 5 minutes
max unresolved questions = 5
```

For a new high-confidence `knowledge_update` or `correction`:

```text
0 plausible recent questions
    -> standalone factual update

1 plausible recent question
    -> pair with it

>1 plausible recent questions
    -> ambiguous: do NOT pair automatically
       index as standalone factual update only if its classifier confidence
       independently satisfies factual-evidence threshold
```

A newer question supersedes no older question automatically.

Do not use sender identity as proof that a message is an answer.

---

## 10.3 Optional cheap compatibility score

Only if needed to disambiguate one candidate from several, use already available embeddings:

```text
cosine(question_embedding, answer_embedding)
```

Do not create a new embedding call solely for pairing.

Accept only if:

```text
best similarity clearly exceeds second-best by >= 0.10
```

Otherwise do not pair.

This step is optional in implementation only if multiple-question ambiguity is already handled conservatively by refusing to pair.

Do not add a learned pairing model in this pass.

---

# 11. Retrieval eval expansion

Keep all existing cases in:

```text
evals/retrieval.yaml
```

Expand retrieval evaluation to **500 total queries**.

Create deterministic synthetic paraphrases from existing known Q&A anchors.

Each generated case must contain:

```text
query
expected_anchor
optional also_accepts
source = synthetic
```

Do not invent new factual answers or anchors.

Generate paraphrases only for anchors that already exist in the seed knowledge base.

Include:

- Catalan;
- Spanish;
- mixed language;
- typos;
- short queries;
- lexical variants;
- paraphrases with little token overlap;
- queries with strong exact identifiers/names;
- similar competing intents.

No generated retrieval test case may appear in classifier training data.

---

# 12. Evaluation metrics

Create/update local evaluation script to report:

## Classifier

On the fixed 500-case test set:

```text
accuracy
macro F1
precision/recall/F1 per class
confusion matrix
coverage = fraction not ambiguous
precision among non-ambiguous predictions
```

Primary metric:

```text
macro F1
```

Safety metric:

```text
precision(knowledge_update + correction)
```

---

## Retrieval

Compare **before vs after**:

```text
baseline:
    existing semantic retrieval,
    current question+answer vectors where reproducible

new:
    question-focused semantic + BM25 RRF
```

Report:

```text
Recall@1
Recall@3
Recall@5
MRR
```

Also report separately:

```text
Catalan
Spanish
synthetic typo/paraphrase subset
```

---

## Listener

Create at least 100 deterministic listener scenarios covering:

```text
explicit reply
single unresolved question
multiple unresolved questions
standalone update
correction
chitchat
ambiguous classifier output
cross-space isolation
```

Report:

```text
pair precision
pair recall
factual-index precision
question detection precision/recall
```

Primary listener metric:

```text
factual-index precision
```

Favor precision over recall.

---

# 13. Acceptance thresholds

Do not silently tune against the test set.

Classifier:

```text
macro F1 >= 0.90
knowledge_update precision >= 0.92
correction precision >= 0.92
```

Retrieval:

```text
Recall@3 >= 0.95
Recall@5 >= 0.98
MRR must improve over semantic-only baseline
```

Listener:

```text
factual-index precision >= 0.95
pair precision >= 0.95
```

If a threshold fails:

- do not weaken it automatically;
- report the failure;
- identify the main confusion categories;
- keep the implementation if it still improves baseline, but clearly mark the gate as failed.

---

# 14. Cloudflare quota rule

All development/training/evaluation in this pass is local.

Allowed:

```text
Ollama embeddinggemma
SQLite
NumPy
scikit-learn training script
local FastAPI
local tests
```

Forbidden during implementation:

```text
Cloudflare Workers AI
Cloudflare live eval
remote reindex
remote seed
remote smoke
production deployment
```

Production runtime should use no more AI calls than before.

Expected effect:

```text
classifier cold start:
before -> message + prototype embeddings
after  -> message embedding only
```

So classifier embedding usage should decrease.

BM25 adds SQL work only, no AI quota.

---

# 15. Required files / outputs

At the end, repository must contain:

```text
scripts/generate_classifier_dataset.py
scripts/train_classifier.py
data/classifier/model.json

500-case classifier train split
500-case classifier test split

500-case retrieval eval

runtime linear classifier
FTS5 projection
RRF hybrid retrieval
conservative deterministic listener

reports/retrieval-classifier-listener.md
```

Use existing dataset filenames when they already exist.

Do not duplicate train/test files under new names unnecessarily.

---

# 16. Final report

Generate:

```text
reports/retrieval-classifier-listener.md
```

Keep it concise.

Required sections:

```markdown
# Retrieval / classifier / listener experiment

## Dataset
- train cases
- test cases
- synthetic/human counts
- language mix

## Classifier
| metric | result |
...

Confusion matrix

## Retrieval
| metric | semantic baseline | hybrid |
...

## Listener
| metric | result |
...

## Runtime cost
- Cloudflare AI calls added: 0
- BM25 SQL query added: yes
- classifier runtime model calls added: 0

## Decision
- thresholds passed / failed
- regressions
- recommended production rollout status
```

Do not write subjective prose without metrics.

---

# 17. Final local gate sequence

Run:

```bash
make format
make lint
make test
make test-integration

uv run python scripts/generate_classifier_dataset.py
uv run python scripts/train_classifier.py

uv run python -m evals.run offline
make test-e2e-local
```

Then run the new classifier/retrieval/listener eval command(s).

All training/evaluation must run without Cloudflare credentials.

---

# 18. Final implementation rules

Do not:

- fine-tune GLM;
- fine-tune EmbeddingGemma;
- add another embedding model;
- add an LLM reranker;
- add query rewriting;
- add HyDE;
- add more generation calls;
- add another database/search service;
- use the 500-case test split for training;
- regenerate the test split between experiments;
- optimize thresholds directly on the test split.

The intended final system is:

```text
QUESTION-FOCUSED EMBEDDINGS
        +
SEMANTIC VECTOR SEARCH
        +
BM25 / FTS5
        +
RRF
        +
ONE LOCALLY TRAINED LINEAR CLASSIFIER HEAD
        +
CONSERVATIVE DETERMINISTIC LISTENER
        +
UNCHANGED GLM-4.7-FLASH ANSWERING
```

Implement exactly that, measure it locally, and report the numbers.
