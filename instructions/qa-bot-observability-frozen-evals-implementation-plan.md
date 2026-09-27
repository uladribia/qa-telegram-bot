# Knowledge Bot — Failure Attribution, Logfire, Frozen Evals, Gold Eval Repair

## Goal

Implement a narrow observability and evaluation hardening pass for `uladribia/qa-telegram-bot`.

The objective is **not** to redesign retrieval, compare models, add dashboards, add an LLM judge, or expand the architecture.

The objective is to make every answer/abstention diagnosable and to make the existing evals trustworthy.

Primary outcomes:

1. A user-visible abstention must no longer hide whether it came from:
   - no usable evidence,
   - an explicit model abstention,
   - malformed model output,
   - invalid citations/source ids,
   - or model/provider unavailability.
2. Add Logfire in local and production, with the same semantic trace structure. Content capture is gated behind an explicit testing flag: with the flag off, no raw user/model content reaches hosted telemetry; with it on, content is exported deliberately so a test deployment's flows can be reconstructed. (Amended 2026-09-27 by owner decision; see "Content capture is a gated testing mode" below.)
3. Add a frozen-evidence generation eval that isolates the generator from retrieval.
4. Repair and simplify the human gold evals.
5. Delete obsolete logging/config/eval paths where the new implementation supersedes them.

---

# Mandatory working rules

## 1. Inspect current `main` before changing anything

This repository may have changed since this plan was written.

Before implementation:

- inspect the current versions of all files named below;
- inspect recent changes touching answer generation, logging, evals, settings, and entrypoints;
- identify which requested changes are already implemented;
- **do not reimplement, duplicate, or revert working equivalent functionality**;
- adapt this plan to the current code while preserving its intended end state.

If the current implementation already satisfies an acceptance criterion, keep it.

Do not create parallel abstractions merely because file/function names differ from this plan.

## 2. Prefer deletion over addition

When new behavior supersedes old behavior:

- delete old code;
- delete stale settings;
- delete obsolete tests;
- delete duplicate logs;
- delete dead enum values;
- consolidate eval files where possible.

Do not preserve compatibility with internal prototype APIs/config that are not used.

## 3. Keep scope narrow

Do **not**:

- change retrieval architecture;
- reintroduce BM25, rerankers, thresholds, or direct-answer logic;
- compare or change generation models;
- add Pydantic Evals;
- add an LLM-as-judge;
- add database tables/columns solely for observability;
- add dashboards or alerting;
- add another logging framework beside Logfire;
- export raw Telegram/group content to hosted telemetry **with the testing flag off** (with it on, see "Content capture is a gated testing mode");
- regenerate hundreds of synthetic examples;
- tune expected eval labels until tests turn green.

---

# Phase 1 — Preserve the real answer failure reason

## Problem

Historically both Ollama and Workers AI adapters could turn malformed/unparseable model output into:

```python
GenerationOutput(status="insufficient")
```

That collapses real model abstention and adapter/model-output failure into the same user-visible abstention.

Fix this first.

## Target semantics

Keep user-facing answer modes small:

- `synthesis`
- `abstention`
- `unavailable`

If `AnswerMode.DIRECT_QA` still exists but is unused, delete it and update stale tests/usages.

Add one small internal reason enum/type. Use these exact semantic reasons unless the current code already has an equivalent:

```text
answered
no_evidence
model_insufficient
invalid_model_output
invalid_source_ids
model_unavailable
```

Do not explode this into dozens of domain reasons.

Fine-grained parser/provider details belong in trace attributes, not the domain reason.

## Adapter behavior

Inspect:

- `src/knowledge_bot/infrastructure/local/ollama.py`
- `src/knowledge_bot/infrastructure/cloudflare/workers_ai.py`
- `src/knowledge_bot/ports/generator.py`
- `src/knowledge_bot/domain/errors.py`

Required end state:

### Explicit model abstention

Valid model output:

```json
{
  "status": "insufficient",
  "answer": "",
  "source_ids": []
}
```

must remain a normal `model_insufficient`.

### Malformed output

These must **not** become `GenerationOutput(status="insufficient")`:

- no model content;
- no JSON object;
- invalid JSON;
- JSON that fails `GenerationOutput` validation;
- truncated unusable output;
- any other parse/schema failure.

Introduce one narrow exception such as:

```python
class InvalidModelOutputError(RuntimeError):
    ...
```

It may carry a small safe code, for example:

- `missing_content`
- `no_json`
- `schema_validation`

Keep codes few and stable.

Do not put raw model output in the exception.

### Provider/model failure

Timeouts, HTTP/provider failures, quota/outage, etc. remain `ModelUnavailableError`.

### Ollama retry

If the local adapter still retries malformed output with an extra prompt like “return valid JSON”, delete that retry.

Ollama is already being asked for structured output. A hidden second LLM call makes failures harder to attribute and differs from production behavior.

One generation request should correspond to one generation span.

---

# Phase 2 — Make `AnswerService` the single decision mapper

Inspect:

- `src/knowledge_bot/application/answer_question.py`
- `src/knowledge_bot/application/answer_policy.py`
- related tests/fakes.

Add the reason to the internal answer outcome/preview, but **do not add a database column just for this**.

Desired mapping:

| Situation | Mode | Reason |
|---|---|---|
| Valid grounded answer | `synthesis` | `answered` |
| No evidence survives selection | `abstention` | `no_evidence` |
| Model validly returns `insufficient` | `abstention` | `model_insufficient` |
| Model output cannot be parsed/validated | `abstention` | `invalid_model_output` |
| Model returns source IDs not supplied to it | `abstention` | `invalid_source_ids` |
| Embedding/generation provider unavailable | `unavailable` | `model_unavailable` |

The user-facing text may remain unchanged.

In particular:

- malformed model output should still fail safe to the normal abstention text;
- the diagnostic distinction must survive internally and into telemetry/eval output.

Expose the reason in `/internal/eval/answer`.

Do not expose internal diagnostic reason to ordinary Telegram users unless the current product explicitly requires it.

---

# Phase 3 — Replace ad-hoc logging with Logfire

## Dependency

Add `logfire`.

**Remove `loguru` unconditionally.** Delete it from dependencies, migrate every remaining meaningful event to Logfire or the standard library, and delete its sink configuration. There is no "if it is only serving the observability role" escape hatch: the goal of this phase is one logging system, and leaving a second dependency behind is a failure of the phase. The two-system overlap that existed while instrumentation was being built is over.

Do not run two logging systems.

Reuse `src/knowledge_bot/infrastructure/logging.py` if it still exists. Do not create a second observability configuration module unless structurally necessary.

## Logfire account/token

Hosted Logfire requires a Logfire project/account and a write token.

Use:

- one Logfire project;
- `environment=local` for local;
- `environment=production` for Cloudflare.

Local development must work with **no Logfire account/token**.

Use the SDK/configuration equivalent of:

```text
send_to_logfire = if-token-present
```

or the current supported API that gives the same behavior.

Tests and CI must not require network access or a Logfire token.

## Configuration

Add optional environment configuration, minimally:

```text
LOGFIRE_TOKEN=
```

If a service/environment setting is required, prefer deriving it from the existing runtime instead of adding more env vars.

### Local

Update `.env.local.example` with an optional `LOGFIRE_TOKEN=`.

Without it:

- application runs normally;
- telemetry remains local/console only.

With it:

- the same semantic traces are exported to Logfire.

### Cloudflare

Configure production using a Cloudflare secret:

```text
LOGFIRE_TOKEN
```

Do not commit it to `wrangler.jsonc`.

Important: Worker bindings are available at request/scheduled runtime, not safely assumed at Python module import time.

If current entrypoint architecture still works this way, initialize/configure hosted Logfire once when the first runtime context is resolved, using `env.LOGFIRE_TOKEN`.

Do the equivalent for the scheduled entrypoint if it can initialize separately.

Logfire initialization/export failure must never break the bot's answer path.

---

# Phase 4 — Trace only what is useful

Do not blanket-instrument everything in this pass.

In particular, do not enable broad request/Pydantic/HTTPX auto-capture if it would capture private request content.

The main semantic trace is the answer pipeline itself.

## Trace structure

One question should produce approximately:

```text
answer_question
  retrieval
  evidence_selection
  generation
  answer_decision
```

Use these names unless current code already has a clearly equivalent naming convention.

### `answer_question`

Safe attributes only:

- stable answer/request/message identifier when available;
- runtime/environment;
- scope id/key if safe;
- question character count;
- final mode;
- final reason.

Do not send raw question text to hosted Logfire **unless the testing content flag is on** — see "Content capture is a gated testing mode" below.

### `retrieval`

Record:

- embedding model;
- duration;
- number of Q&A candidates;
- number of message candidates;
- returned candidate identifiers/anchors;
- similarity score per candidate;
- authority;
- source kind.

This is where retrieval similarity belongs.

Do **not** add similarity scores to the LLM prompt as part of this work.

### `evidence_selection`

Record:

- configured floor;
- candidate count before;
- selected count after;
- selected IDs;
- whether this step caused `no_evidence`.

### `generation`

Record:

- generation model;
- evidence IDs;
- evidence authorities;
- evidence count;
- prompt/request character count;
- duration;
- provider completion/finish reason if available;
- whether content existed;
- parse status;
- model-declared `status`;
- invalid-output safe code when relevant.

Do not send **with the testing content flag off**:

- raw prompt;
- raw evidence text;
- raw user question;
- raw answer;
- raw provider response;
- Telegram username;
- phone/email;
- tokens/secrets.

Tokens and secrets are never sent, in either mode.

### `answer_decision`

Record:

- mode;
- reason;
- cited source IDs.

This should make it trivial to group abstentions by reason.

## Existing logs

Inspect all current structured logs.

Delete logs whose information is fully represented by the new spans, especially duplicate AI-call timing/success logs.

Keep only meaningful non-answer events that do not naturally live under the answer trace, e.g.:

- scheduled job lifecycle/failure;
- delivery failure;
- major background job failure.

Emit them through Logfire.

Avoid log spam.

---

# Phase 5 — Frozen-evidence generator eval

## Goal

Create an eval that answers:

> Given exactly this question and exactly this evidence, did the generator make the correct answer/abstain decision and cite valid evidence?

Retrieval must not participate.

Do not build a new eval framework.

Reuse `evals/run.py`, the existing `GenerationRequest`, existing answer decision path, and the existing internal eval endpoint where practical.

## Endpoint

Extend `/internal/eval/answer` rather than creating a parallel service if the existing endpoint remains appropriate.

Add an optional frozen evidence payload to its request contract.

When absent:

- preserve the normal live end-to-end eval behavior.

When present:

- bypass retrieval;
- construct `Evidence` / `GenerationRequest` from the supplied frozen evidence;
- run the same `AnswerService.decide()` behavior used by production after retrieval.

Give frozen evidence a similarity that unambiguously survives the current deterministic floor, e.g. `1.0`, unless the current implementation provides a cleaner way to invoke the post-retrieval decision path.

The frozen path must not contain a second generator implementation.

## Frozen dataset

Create one small human-curated dataset, e.g.:

```text
evals/frozen_generation.yaml
```

Target roughly 30–40 cases.

Do not generate hundreds of synthetic cases.

Cover:

1. one clearly sufficient source;
2. multiple candidates where only one answers;
3. two complementary sources required together;
4. strong topical near-miss without the requested fact;
5. irrelevant candidates with high apparent topical overlap;
6. equal-authority material conflict -> abstain;
7. stronger-authority correction vs weaker conflicting evidence;
8. Spanish question with Catalan evidence;
9. short/ambiguous question;
10. evidence containing prompt-like/instruction text that must not override system behavior;
11. evidence that supports only part of a multi-part question -> abstain unless the expected answer is explicitly partial;
12. valid answer that requires citing more than one source where appropriate.

Keep adapter/parser failure cases in unit tests, not in the semantic frozen dataset.

## Case shape

Use a deterministic schema close to:

```yaml
- id: equipment_order
  question: "Com demano la roba?"
  evidence:
    - source_id: "qa-equipament-com-demanar"
      label: "Q&A"
      authority: 90
      text: "..."
  expected:
    mode: synthesis
    source_ids:
      - "qa-equipament-com-demanar"
    must_include:
      - "talla"
      - "delegat"
    must_not_claim:
      - "botiga del Barça"
```

For abstentions:

```yaml
expected:
  mode: abstention
```

No LLM judge.

## Metrics/report

At minimum report separately:

- total;
- correct mode/decision;
- false answers;
- false abstentions;
- invalid model output;
- invalid citations/source ids;
- required-fact failures;
- forbidden-claim failures.

Do not merge parser failures into model abstentions.

---

# Phase 6 — Repair and simplify human gold evals

## Consolidate

Inspect:

- `evals/answers.yaml`
- `evals/abstention.yaml`
- `evals/conflicts.yaml`
- `evals/abstention_synthetic.yaml`
- `evals/retrieval.yaml`
- `evals/retrieval_synthetic.yaml`
- current equivalents if files changed.

Prefer one human-curated end-to-end gold file, e.g.:

```text
evals/gold.yaml
```

After migration, delete obsolete human-answer/abstention files rather than keeping multiple competing sources of truth.

Constructed conflict cases belong in the frozen-generation suite, not the end-to-end retrieval gold suite.

## Known bad case to repair

If it still exists, fix the contradiction around:

```text
"On és la seu del club exactament?"
```

It should not simultaneously be documented as answerable and then forced through an abstention suite.

Use the actual current knowledge corpus to decide the final gold label.

## Gold answerable cases

Each answerable gold case should have:

- unique `id`;
- real user-like question;
- `expected_mode: synthesis`;
- expected source anchor/id when deterministic;
- a small number of essential `must_include` checks;
- `must_not_claim` only for materially dangerous/wrong claims.

Do not make `must_include` broad stylistic phrase matching.

These should be facts whose omission means the answer failed its purpose.

## Gold abstention cases

Each should have:

- unique `id`;
- question;
- `expected_mode: abstention`;
- human-readable reason/provenance.

Do not invent a fake answer string for an abstention unless needed for documentation only.

## `must_include`

Change gold/live evaluation behavior so missing required facts **fails** the case.

It must no longer be only an advisory warning.

Keep required facts sparse enough that this hard gate is fair.

## Delete arbitrary size gates

Remove checks such as:

```text
answers must contain >= N cases
abstentions must contain >= N cases
```

if they exist only to enforce volume.

Replace with:

- schema validation;
- unique IDs/questions;
- valid modes;
- valid referenced anchors;
- provenance;
- no duplicate normalized questions.

Do not add synthetic filler merely to hit a target count.

---

# Phase 7 — Keep synthetic evals separate from release gold

Synthetic data can remain useful for local experiments.

Do not delete useful synthetic retrieval/classifier stress data solely because it is synthetic.

But:

- default production live answer gate should use human gold;
- default production live abstention gate should use human gold;
- default production live retrieval gate should use human gold retrieval queries;
- large synthetic suites should not burn Workers AI quota by default.

Keep the existing 467 synthetic retrieval paraphrases for the free local retrieval experiment if still useful.

Report gold and synthetic metrics separately.

Never present one combined headline metric if synthetic cases dominate the denominator.

---

# Phase 8 — Make eval scope match production scope

Inspect `AnswerService.dry_run()`, `retrieve_for_eval()`, and `/internal/eval/answer`.

If the eval path still uses `all_scopes=True`, remove that behavior for normal live gold evaluation.

A normal gold question without a `space_id` should exercise the same global-only retrieval semantics as a normal global question.

If a future gold case explicitly tests local/group knowledge, let the case pass a specific `space_id`.

Do not search all groups/scopes for eval convenience.

Expose enough retrieval metadata from `/internal/eval/answer` to attribute failures, including stable anchor/source ID and similarity where appropriate.

---

# Phase 9 — Automatic failure attribution in eval output

For each failed gold case, make the report distinguish at least:

```text
retrieval_miss
model_false_abstention
invalid_model_output
invalid_source_ids
answer_content_failure
unexpected_answer
model_unavailable
```

Use the actual pipeline evidence/reason to derive this.

Examples:

### Retrieval failure

Expected anchor not in retrieved candidates:

```text
retrieval_miss
```

### Generator failure

Expected anchor was retrieved, but final reason is `model_insufficient`:

```text
model_false_abstention
```

### Model/adapter format failure

Expected anchor was retrieved, but final reason is `invalid_model_output`:

```text
invalid_model_output
```

### Grounding failure

Model answered with a source ID not supplied:

```text
invalid_source_ids
```

### Completeness failure

Correct supported answer/citation but missing an essential `must_include` fact:

```text
answer_content_failure
```

This is one of the core outputs of the project.

---

# Phase 10 — Delete stale settings and architecture remnants

Inspect `.env.example`, `.env.local.example`, `Settings`, docs and tests.

Delete stale settings that no longer correspond to actual `Settings` fields or current architecture.

Known candidates from the previous repository state included:

```text
DIRECT_QA_THRESHOLD
SYNTHESIS_THRESHOLD
PAIRING_WINDOW_MINUTES
PAIRING_QUIET_MINUTES
PAIRING_OVERLAP_MINUTES
AI_PAIRING_TIMEOUT_SECONDS
LOG_LEVEL
```

Do not delete blindly: first verify current usage.

If unused, remove from examples/docs/tests.

Align local pipeline configuration with production where that is configuration rather than model choice:

```text
ANSWER_SIMILARITY_FLOOR
QA_TOP_K
MESSAGE_TOP_K
```

Do not change models as part of this work.

If `KB_LOG_CONTENT` / `Settings.log_content` still exists only as a switch for raw logging, delete it. It has no reason to exist once Loguru is gone.

### Content capture is a gated testing mode

Content capture already exists in the codebase as `KB_LOGFIRE_CAPTURE_CONTENT` (default `true` at time of writing). Keep it, and keep it explicit:

- **Flag off (the invariant):** no raw user content, prompt, answer, evidence
  body, or Telegram identity in hosted telemetry. Only ids, counts, sizes,
  similarities, modes, reasons, and timings.
- **Flag on (test deployments only):** the same traces plus content, so a
  question that arrived, what the model was asked, what it replied, and what
  the bot sent back can be reconstructed end to end.

Safeguards that hold in **both** modes:

- tokens, secrets, webhook secrets, and API keys are never exported; the
  transport's bot token is scrubbed by value shape, not only by key name;
- tests and CI never export, whatever the flag says;
- telemetry failure never breaks answering.

The flag defaults to `true` while this project is an explicitly-flagged test
deployment. Turning it off must be a one-setting change, and turning it off is
required before connecting anything but a private test group. Do not add a
second content switch, and do not make content capture unconditional in code.

---

# Phase 11 — Tests

Add or update the smallest tests necessary.

## Required unit coverage

### Generator adapters

For both Ollama and Workers AI equivalents:

1. valid answered JSON -> `GenerationOutput(answered)`;
2. valid insufficient JSON -> `GenerationOutput(insufficient)`;
3. missing content -> `InvalidModelOutputError`;
4. no JSON -> `InvalidModelOutputError`;
5. invalid schema -> `InvalidModelOutputError`;
6. provider timeout/error -> `ModelUnavailableError`.

No malformed-output-to-insufficient fallback.

### Answer service

Test each semantic reason:

- `answered`;
- `no_evidence`;
- `model_insufficient`;
- `invalid_model_output`;
- `invalid_source_ids`;
- `model_unavailable`.

### Eval endpoint

Test:

- normal retrieval-backed mode;
- frozen-evidence mode;
- `reason` present;
- evidence/source metadata sufficient for attribution;
- frozen mode really bypasses retrieval.

### Logging privacy

Where practical, test the attributes passed to observability helpers.

Test both modes:

- content flag off: raw question/evidence/answer text and Telegram identity
  are absent from the attributes the helpers pass;
- content flag on: the content events carry the text, and the credential
  scrubbing still holds.

Do not test Logfire's own internals.

## Remove obsolete tests

Delete tests for:

- `DIRECT_QA` if mode is dead;
- fake conflict behavior superseded by frozen generation;
- old logging event names that no longer exist;
- stale config fields.

Do not keep tests solely to preserve deleted prototype behavior.

---

# Phase 12 — Documentation

Update only relevant docs.

At minimum:

- `docs/operations.md`
- `docs/development.md`
- `.env.example`
- `.env.local.example`
- README only if commands/workflow changed materially.

## Operations troubleshooting

Replace obsolete “tail is the only history” instructions.

Primary debugging procedure should become:

1. locate `answer_question` trace;
2. inspect final `reason`;
3. if `no_evidence`, inspect `retrieval` + `evidence_selection`;
4. if `model_insufficient`, inspect frozen/gold evidence and generation span;
5. if `invalid_model_output`, inspect parse/provider metadata;
6. if `model_unavailable`, inspect provider error/timeout;
7. if `invalid_source_ids`, inspect generation result and supplied IDs.

Document the privacy rule: credentials never, content only behind the testing flag.

Document how to configure optional local Logfire export and required production token.

Do not write a long observability architecture document.

---

# Commands / interface

Reuse existing commands where possible.

Prefer extending the current eval CLI over introducing multiple scripts.

Desired conceptual commands:

```bash
uv run python -m evals.run offline
uv run python -m evals.run live --suite gold --base-url ...
uv run python -m evals.run live --suite frozen --base-url ...
```

Names can differ if the current CLI already has a better compatible structure.

Keep large synthetic retrieval/classifier experiments under the existing local/free eval command.

Do not make hosted Logfire a prerequisite for evals.

---

# Acceptance criteria

The implementation is complete only when all of the following are true.

## Failure semantics

- Every answer outcome has exactly one semantic reason.
- Malformed/unparseable model output can never appear as `model_insufficient`.
- Explicit valid `insufficient` remains distinct.
- Invalid source IDs remain distinct.
- Provider failures remain distinct.
- User-facing behavior remains safely abstaining/unavailable.

## Logfire

- Local and production use the same semantic trace/span names.
- Local runs correctly without a Logfire token/account.
- Production can export using `LOGFIRE_TOKEN`.
- Failure of telemetry/export cannot break answering.
- With the content flag **off**: no raw question text, evidence text, generated
  answer, provider response, or Telegram identity is exported.
- With the content flag **on**: content events are exported and the flows are
  reconstructable, and credentials are still scrubbed.
- No tokens/secrets are exported in either mode.
- `loguru` is gone from dependencies, with every meaningful event migrated.
- Duplicate old AI timing logs are removed where superseded.

## Frozen eval

- Frozen eval bypasses retrieval.
- It uses the same answer/generation decision path as production.
- Dataset is small and human-curated.
- No LLM judge.
- Parser/adapter failures are reported separately from semantic abstention.

## Gold eval

- Human gold answer + abstention expectations have one clear source of truth.
- Known contradictory cases are repaired.
- Gold `must_include` failures are hard failures.
- Arbitrary dataset-size gates are removed.
- Synthetic cases do not dominate/default the production live release gate.
- Retrieval gold and synthetic results are reported separately.

## Eval attribution

A failed gold case can be classified without reading raw production logs as one of:

- retrieval miss;
- model false abstention;
- invalid model output;
- invalid source ids;
- answer content failure;
- unexpected answer;
- model unavailable.

## Cleanup

- Dead `DIRECT_QA` code removed if still unused.
- Stale env/settings removed if still unused.
- Obsolete fake conflict eval removed after frozen equivalent exists.
- Old duplicate logging removed.
- No parallel evaluation framework introduced.
- No model comparison implemented.

---

# Validation before handoff

Run the repository's current equivalent of all applicable checks.

At minimum, if commands still exist:

```bash
make format
make lint
make test
make test-integration
uv run python -m evals.run offline
make eval-local
```

Then exercise:

1. local answer trace with no Logfire token;
2. local answer trace with Logfire token if credentials are available;
3. local frozen suite;
4. local gold suite if supported;
5. production readiness/smoke;
6. one production answer trace exported to Logfire;
7. production human gold suite;
8. production frozen suite.

Do not repeatedly burn Cloudflare AI quota while debugging deterministic/offline failures.

---

# Final deliverable from the implementing agent

At completion, return a concise report containing:

1. files changed;
2. files deleted;
3. features from this plan that were already present and therefore left untouched;
4. final failure-reason taxonomy;
5. Logfire local/prod setup steps;
6. gold eval case count;
7. frozen eval case count;
8. local eval results;
9. production gold/frozen results if actually run;
10. any remaining failures, categorized by the new attribution taxonomy.

Do not hide failing evals.

Do not weaken expected results merely to make the suite pass.
