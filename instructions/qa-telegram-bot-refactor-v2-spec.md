# Knowledge Bot v2 — Correctness-First, Local-First Refactor Specification

**Repository:** `uladribia/qa-telegram-bot`
**Delivery:** small phase-group branches, merged in canonical order
**Audience:** coding agents (DeepSeek-class) and human maintainers
**Status:** implementation contract
**Primary goals:** correctness, maintainability, channel independence, fully local development, zero paid production dependencies
**Last reviewed against repository:** 2026-09-24

---

# 0. How to use this document

This document is not a brainstorming note. It is the implementation contract for the refactor.

The implementing agent MUST follow the decisions in this document unless a step is technically impossible. If a step is impossible, the agent MUST stop that phase, document the exact blocker with evidence, and propose the smallest compatible change. It MUST NOT silently replace a decision with a different architecture.

The agent MUST NOT redesign the product while implementing this plan. In particular, it MUST NOT add new frameworks, new databases, an agent framework, a paid provider, a second vector database, ONNX, FAISS, LangChain, PydanticAI, SQLAlchemy, Redis, queues, microservices, Docker Compose, or a web frontend.

All implementation phases must keep the repository green. A commit may contain a regression test and its fix together; do not commit an intentionally failing test to the branch.

The implementation is allowed to break internal APIs. This is a prototype. Do not preserve obsolete APIs through compatibility shims.

## 0.1 Canonical implementation order

Implement in this order and do not reorder large phases:

1. Lock behavioral invariants and add regression coverage.
2. Introduce channel-independent spaces, principals, and scope resolution.
3. Fix Q&A identity/provenance and stable search projection IDs.
4. Fix background ingestion/classification/indexing semantics.
5. Simplify answering and AI orchestration.
6. Refactor correction/reviewer workflows and permissions.
7. Introduce the generic stateless REST boundary and thin Telegram adapter.
8. Add the fully local Docker runtime: SQLite + NumPy + Ollama.
9. Add the Cloudflare composition root and real scheduled daily report.
10. Split oversized infrastructure/route modules only after behavior is correct.
11. Rewrite documentation and bot self-Q&A.
12. Run the final test matrix and the explicitly authorized Cloudflare smoke path.

Do not start with file moves. Correctness precedes cosmetics.

Deliver the ordered phases through small reviewable branches rather than one long branch or one oversized pull request. The planned branch groups are:

1. `refactor/v2-correctness` — Phases 0–3;
2. `refactor/v2-learning-review` — Phases 4–6;
3. `refactor/v2-boundaries-report` — Phase 7;
4. `refactor/v2-local-runtime` — Phase 8;
5. `refactor/v2-production-docs` — Phases 9–12.

Each branch starts from the updated `main` after the previous group has been merged with `--no-ff`.

## 0.2 Data-retention decision

The only knowledge that must survive the v2 cutover is the versioned source material already committed under `data/`, especially `data/seed/`. Existing D1 corrections, messages, group context, reviewer assignments, and search projections are disposable experiments and do not require production-data preservation.

The v2 schema and migration behavior must still be tested against realistic temporary fixtures, but the production cutover may recreate D1 and Vectorize from migrations plus committed seed data. Do not block the refactor on a complex in-place preservation of disposable D1 rows.

---

# 1. Product model to preserve

The bot is a small, evidence-grounded knowledge system for private groups. Expected traffic is low: approximately 20–30 directly addressed questions per day. The system must therefore optimize for correctness and operational simplicity, not distributed-system scale.

## 1.1 One source of truth, two knowledge scopes

There is exactly **one authoritative data store**:

- local development: SQLite;
- production: Cloudflare D1.

There are exactly **two kinds of knowledge scope**:

1. `global`: shared by every space;
2. `space:<space_id>`: visible only inside one logical space.

Do not describe these as “two sources of truth”. They are two layers inside one authoritative knowledge model.

Vector indexes are derived projections. They are never authoritative.

## 1.2 A space is not a Telegram chat

A **space** represents the logical group/community context. A Telegram group is one possible binding to that space.

Example:

```text
Space sp_abcd1234
├── Telegram group -1001234567890
├── future WhatsApp group wa:...
└── future web application membership
```

Local knowledge belongs to the space, not to a Telegram chat id.

This is mandatory because future channels must be able to see the same local knowledge without duplicating it.

## 1.3 Roles

There are three authority roles:

- **local reviewer**: one optional reviewer per space;
- **global reviewer**: one optional reviewer for global knowledge;
- **admin**: configured operational principal with full authority.

Any ordinary group member may report a wrong answer and propose a correction.

### Permission matrix

| Action | Local reviewer | Global reviewer | Admin |
|---|---:|---:|---:|
| Review correction originating in own space | yes | yes as fallback | yes |
| Edit correction proposal in own review flow | yes | yes | yes |
| Reject correction | yes, own space | yes | yes |
| Approve as local to origin space | yes, own space | yes | yes |
| Approve as global | **no** | yes | yes |
| Nominate/remove local reviewer | no | no | yes |
| Nominate/remove global reviewer | no | no | yes |
| Roll back approved knowledge | no | no | yes, operational endpoint/CLI |

A local reviewer MUST NOT be able to approve global knowledge, even by crafting a callback manually. This must be server-side authorization, not only a hidden button.

## 1.4 Event-driven behaviors

The runtime has three independent event families:

### A. User question event

Triggered only when a channel says the bot was explicitly addressed.

```text
channel event
→ normalize
→ resolve space/principal
→ persist inbound message
→ retrieve evidence
→ direct answer OR one grounded synthesis OR abstain
→ persist BotAnswer
→ channel adapter delivers result
```

### B. Background conversation event

Triggered by an unaddressed message only when background listening is enabled.

```text
channel event
→ normalize
→ resolve space/principal
→ persist raw message
→ deterministic prefilter
→ embedding classifier if required and budget allows
→ classify
→ index only if the message is actual evidence
```

The background path MUST NOT answer the group.

### C. Daily scheduled event

Triggered once per day.

```text
scheduled event
→ deterministic DailyReportService
→ summarize addressed questions + background questions + corrections + ingestion + AI usage
→ send one admin report
```

The daily report MUST NOT call a generative model.

## 1.5 Channel independence

Telegram is an adapter, not the application.

The core must not contain concepts such as:

- Telegram callback IDs;
- `ForceReply`;
- inline keyboards;
- Telegram chat IDs as knowledge scopes;
- Telegram user IDs as reviewer identities;
- Telegram-specific commands inside knowledge services.

The core may use opaque `principal_id`, `space_id`, `channel`, and `external_*` identifiers supplied by adapters.

The channel-specific adapter owns parsing external ids, Telegram-specific source identity, and Telegram binding lookup. The application-level space/binding service accepts only opaque channel and external identifiers and contains no Telegram imports, Telegram constants, chat-id parsing, or channel-specific branches. This keeps connector concerns out of group/space management.

The external application boundary is REST + Pydantic contracts. All durable state is stored in the SQL database. No workflow depends on process memory surviving between requests.

Process-local caches are allowed only as performance caches and must be disposable, e.g. cached classifier prototype embeddings.

---

# 2. Current-state audit: defects that MUST be fixed

This section records observed behavior in the current `main` implementation. These are not optional cleanup items.

## 2.1 Direct answers do not persist cited Q&A provenance

`AnswerService.answer()` stores `sources_json` but does not populate `BotAnswer.qa_version_id` for a direct Q&A answer.

Consequence: feedback on a direct answer commonly loses the identity of the Q&A version that produced it.

Required fix:

- a direct Q&A `AnswerOutcome` must carry `qa_item_id` and `qa_version_id`;
- `BotAnswer.qa_version_id` must be populated for direct answers;
- synthesis and abstention keep it `None`;
- feedback starts from the cited Q&A item when one exists.

## 2.2 Q&A identity is inconsistent across ingestion paths

Current web seeding uses the web anchor as `qa_items.canonical_key`, while new corrections can derive a key from the user question text.

Consequence: a correction may create a second Q&A rather than a variant/successor of the original one. Local-over-global suppression then fails.

Required fix is specified in Section 7: separate semantic canonical identity from source provenance and migrate existing rows.

## 2.3 Old Q&A vectors remain searchable

Current Q&A vector ids are version ids. Approving a correction inserts a new version and upserts a new vector, but the previous version vector is not removed.

A full reindex upserts current records but does not clear stale old vectors.

Consequence: superseded answers can remain retrievable.

Required fix:

- Q&A vector id must be stable per Q&A item: `qa:<qa_item_id>`;
- updating the current version upserts the same vector id;
- full rebuild must delete the known derived projection before re-creating it;
- one-time v2 migration must explicitly delete legacy version-id vectors.

## 2.4 Scope filtering is not completely provisioned

Retrieval filters Vectorize on scope, but current setup documentation only instructs creating metadata indexes for `kind` and `status`.

Required production Vectorize metadata indexes after v2:

```text
kind
status
scope_key
```

No deployment is considered valid until all three exist.

## 2.5 Background listener stores but does not incrementally index new evidence

The current background flow classifies and ingests a message, then stops. The message is searchable only after a later full reindex.

Required fix:

- relevant background evidence is indexed immediately;
- standalone questions are reported but never indexed as factual evidence;
- obvious chitchat is never indexed;
- paired question→answer messages are indexed as combined evidence;
- no full reindex is required for routine background learning.

## 2.6 Full message reindex currently indexes every textual message

Current `D1SearchIndexSource.list_messages()` selects every non-empty textual message.

That allows questions and irrelevant conversation to become retrieval evidence.

Required fix: only explicitly eligible evidence can appear in the message search projection.

## 2.7 Reviewer authorization is too permissive

Current `ReviewerRouter.can_confirm()` allows a local reviewer to confirm a correction, while the callback separately chooses local/global approval. Existing tests explicitly exercise a local reviewer using global approval.

Required fix: authorization must include both actor and requested decision scope. Local reviewers may approve only the origin space.

## 2.8 Daily recap does not capture the intended background question set

Current recap is built mainly from `bot_answers`, so it represents directly addressed questions. It does not faithfully represent questions detected in background conversation.

Required fix: one `DailyReportService` must query both addressed questions and background question messages.

## 2.9 Scheduled-job documentation is stale

Current docs say Python Workers have no scheduled handler. Current Cloudflare Python Workers do support `scheduled()` handlers and Cron Triggers.

Required fix:

- replace opportunistic recap/report checks with one actual scheduled handler;
- keep an authenticated manual REST endpoint that executes the exact same job for development/operations;
- remove opportunistic report invocation from message handling.

## 2.10 Integration tests can use disconnected fake repositories

Current test composition creates separate in-memory repository instances for services that should share state.

Consequence: green “integration” tests do not necessarily prove an end-to-end state transition.

Required fix: build one `InMemoryBackend` fixture containing one shared instance of every store, and wire every service from it.

## 2.11 HTTP routes reach through application services

Examples include access patterns equivalent to:

```python
context.answer.retrieval...
context.ingestor.messages...
context.feedback.answers...
```

Required fix: HTTP/channel routes call explicit application use cases only.

## 2.12 Telegram concepts leak into the core

Current application/ports expose operations such as `send_force_reply()` and `answer_callback()`.

Required fix: Telegram interaction rendering/correlation moves to the Telegram adapter. The application exposes generic correction/review operations.

## 2.13 Invalid/empty input paths are brittle

The current reviewer command detection can index `message.text.split()[0]` on an empty string. Internal route bodies also manually coerce values and can turn malformed input into 500 errors.

Required fix:

- all REST bodies use Pydantic request models;
- empty text is valid input and never raises an accidental `IndexError`;
- validation errors are 4xx;
- configuration parsing is fail-fast through Pydantic.

## 2.14 Direct clock use exists outside the clock adapter

Reviewer event creation currently uses `datetime.now(UTC)` directly.

Required fix: all application timestamps come from the injected `Clock` port.

## 2.15 AI/eval logic is unnecessarily expensive

Current live evals can use many model calls and an LLM-as-judge pass. This has already contributed to quota exhaustion.

Required fix:

- no runtime judge;
- no routine live LLM judge;
- local AI E2E becomes the normal semantic development test;
- Cloudflare AI smoke is tiny, explicit, and manually authorized.

## 2.16 Source type is incorrectly used as source instance identity

Current ingestion can use values such as `telegram` or `whatsapp_import` both as source type and as the durable source row id. Distinct imports/scopes can therefore collapse onto one source row, and a later import cannot reliably change scope/provenance.

Required fix:

- `SourceType` remains a closed enum describing the kind of origin;
- `Source.id` is a unique source-instance id;
- Telegram runtime, each web origin, and each WhatsApp import have explicit source instances;
- message indexing reads authority/type from the source row, not by interpreting the source id string.

## 2.17 Documented admin-message authority is not implemented in current message indexing

Current search-index authority for messages is derived from source id/type and does not reliably raise a Telegram admin-authored knowledge update above an ordinary Telegram user message, despite the documentation describing a higher admin authority.

Required fix:

- evidence authority is computed by one domain policy from `source_type` plus `sender_is_admin`;
- approved Q&A = 100;
- admin-authored Telegram evidence = 95;
- published web Q&A = 90;
- WhatsApp import evidence = 50;
- ordinary Telegram evidence = 40;
- under-review web Q&A is not answerable/indexed as active evidence.

---

# 3. Non-negotiable technology choices

## 3.1 Language and quality toolchain

Keep:

- Python 3.13;
- `uv`;
- FastAPI;
- Pydantic v2 + pydantic-settings;
- Typer for operational CLI;
- Loguru;
- Ruff;
- `ty`;
- pytest;
- Docker.

## 3.2 Approved new Python dependencies

The refactor may add only these dependencies without further human approval:

```text
numpy       local vector similarity
aiosqlite   local async SQLite adapter
uvicorn     local FastAPI server
```

`httpx` already exists in the development toolchain and may be used for the local HTTP adapter/client.

Do not add the Ollama Python package. Call Ollama through its local REST API. This keeps Ollama outside the Python dependency graph.

## 3.3 Explicitly forbidden dependencies/technologies

Do not add:

```text
onnxruntime
transformers
torch
sentence-transformers
faiss
chromadb
qdrant
SQLAlchemy
LangChain
LlamaIndex
PydanticAI
Redis
Celery
Docker Compose
Postgres
```

None of them is needed for this workload.

## 3.4 Local AI models — configured defaults

Use these defaults:

```text
embedding model: embeddinggemma
generator model: gemma3:270m
```

Both run in a single local Ollama server/container. Read model names from validated settings rather than hardcoding them in adapters. Settings must reject names outside the explicitly allowed zero-cost model set. The implementing agent must not silently substitute a different model.

`embeddinggemma` is used for:

- question retrieval embeddings;
- background-message classification;
- background evidence indexing;
- Q&A indexing.

`gemma3:270m` is used only for grounded synthesis when no direct Q&A answer is strong enough.

## 3.5 Production AI models — configured defaults

Keep these defaults:

```text
@cf/google/embeddinggemma-300m
@cf/zai-org/glm-4.7-flash
```

Read production model names from validated settings. The allow-list must contain only approved zero-cost Cloudflare Workers AI models, and startup must reject any configured pair outside that allow-list. The coding agent must not substitute production models during the refactor.

## 3.6 Zero-cost rule

Production must retain a hard zero-paid-fallback design.

Do not configure:

- a paid Cloudflare fallback;
- OpenAI/Anthropic/OpenRouter fallback;
- another cloud embedding provider.

When free capacity is unavailable, degrade safely.

---

# 4. Target architecture

## 4.1 Deployment shapes

### Local development

```text
┌──────────────────────────────────────────────────────────────┐
│ Docker network: knowledge-bot-dev                            │
│                                                              │
│  ┌───────────────────────────────┐                           │
│  │ knowledge-bot-local           │                           │
│  │ CPython 3.13                  │                           │
│  │ FastAPI + Uvicorn             │                           │
│  │ SQLite                        │                           │
│  │ NumPy vector store            │                           │
│  │ application/core              │                           │
│  └──────────────┬────────────────┘                           │
│                 │ HTTP REST                                  │
│                 ▼                                            │
│  ┌───────────────────────────────┐                           │
│  │ knowledge-bot-ollama          │                           │
│  │ embeddinggemma                │                           │
│  │ gemma3:270m                   │                           │
│  └───────────────────────────────┘                           │
└──────────────────────────────────────────────────────────────┘
```

No Cloudflare account, token, binding, Vectorize index, D1 database, or Workers AI call is required for canonical local development.

### Production

```text
Telegram / future channel
          │
          ▼
Cloudflare Python Worker
          │
          ├── FastAPI REST + Telegram adapter
          ├── D1
          ├── Vectorize
          └── Workers AI
```

One Worker, one D1 database, one Vectorize index.

## 4.2 Clean architecture dependency rule

```text
domain
  ↑
application
  ↑
ports + contracts
  ↑
adapters / infrastructure
  ↑
entrypoints / composition roots
```

### Domain

Contains:

- ids/value objects;
- enums;
- entities;
- scope and permission rules;
- deterministic policies.

No framework imports.

### Application

Contains:

- use cases;
- orchestration;
- evidence selection;
- background classification/indexing policy;
- correction/review workflows;
- daily report construction.

Application may depend only on domain, contracts that are genuinely internal, and ports.

### Ports

Protocols only at external/I/O seams:

- repositories;
- clock;
- embedder;
- generator;
- vector store;
- notification/delivery;
- transactional correction commit.

Do not define Protocols for pure functions.

### Adapters

Convert external formats to application contracts and back.

Telegram-specific rendering and reply/callback correlation stays here.

### Infrastructure

Concrete storage/model implementations:

- local SQLite;
- local NumPy vector storage;
- local Ollama REST;
- Cloudflare D1;
- Cloudflare Vectorize;
- Cloudflare Workers AI.

## 4.3 Stateless runtime rule

The following must survive process/container/Worker-isolate restart because they live in SQL:

- inbound message deduplication;
- correction state;
- review state;
- reviewer assignments;
- Q&A versions/current pointers;
- adapter interaction correlation needed across requests;
- delivery receipts;
- daily report last-run state;
- AI metering;
- search projection manifest.

The only allowed process-local mutable state is a disposable cache such as classifier prototype vectors.

---

# 5. Identity model

## 5.1 `space_id`

Introduce an internal `space_id` independent from Telegram.

Format for new ids:

```text
sp_<32 lowercase hex chars>
```

Generate with:

```python
f"sp_{uuid.uuid4().hex}"
```

Do not expose a Telegram chat id as a `space_id`.

## 5.2 `scope_key`

Use exactly:

```text
global
space:<space_id>
```

Implement in `domain/scope.py`:

```python
GLOBAL_SCOPE = "global"

def scope_for_space(space_id: str) -> str:
    return f"space:{space_id}"
```

Provide a parser that rejects any other format. Do not use arbitrary strings in new code.

## 5.3 `principal_id`

The core uses an opaque principal string:

```text
<channel>:<external-user-id>
```

Examples:

```text
telegram:12345678
web:user-42
whatsapp:<opaque-id>
```

The core must never parse the provider-specific suffix. It may parse only the channel prefix when delivery routing requires it outside the application layer.

For Telegram:

```python
principal_id = f"telegram:{telegram_user_id}"
```

The configured admin is converted at composition time to:

```text
telegram:<ADMIN_TELEGRAM_USER_ID>
```

No application service receives a raw Telegram user id.

## 5.4 Conversation identity

`conversation_id` is a stored internal conversation record id. It is not a scope.

Channel binding maps:

```text
(channel, external_conversation_id) → conversation_id + space_id
```

Application retrieval uses `space_id`, never `conversation_id`, to decide local knowledge visibility.

## 5.5 Source identity

`SourceType` and `Source.id` are different concepts.

`SourceType` remains one of the closed origin kinds (`web_seed`, `whatsapp_import`, `telegram`, `admin` where still needed). `Source.id` identifies one concrete source instance.

Use deterministic, readable-enough ids:

```text
src:telegram:runtime
src:web:<first-16-hex-of-sha256-normalized-url>
src:whatsapp:<first-16-hex-of-sha256(scope-key + import-fingerprint)>
```

Rules:

- do not use `source_type.value` as the source id;
- one web URL may have one durable source instance across snapshots;
- a WhatsApp import into a different scope is a distinct source instance;
- the Telegram runtime source may be shared because message visibility comes from the resolved space, not the source row;
- `Source.scope_key` describes the source/import provenance where meaningful but never substitutes for message/QA scope.

`MessageIngestor` must receive or resolve a real source instance; it must not fabricate the id from the source type enum.

---

# 6. Canonical Q&A identity and provenance

This section is mandatory because current identity mixing is a root correctness bug.

## 6.1 Semantic canonical key

`qa_items.canonical_key` must represent the semantic canonical question, not a web anchor.

Use this exact normalization:

```python
import hashlib
import unicodedata


def normalize_canonical_question(question: str) -> str:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    return " ".join(normalized.split())


def canonical_key_for(question: str) -> str:
    normalized = normalize_canonical_question(question)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
```

Do not remove punctuation, accents, or words beyond NFKC + casefold + whitespace collapse.

## 6.2 Source anchor is provenance, not identity

Add to `qa_versions`:

```text
source_anchor TEXT NULL
```

For web-seeded versions:

```text
canonical_key = canonical_key_for(question)
source_url     = base/source URL
source_anchor  = exact source anchor
```

Citation rendering constructs the exact anchored URL from `source_url` + `source_anchor`.

Human-approved versions have:

```text
source_url = NULL
source_anchor = NULL
author = proposer/reviewer attribution according to existing policy
```

## 6.3 Cross-scope variants

The same canonical question may exist once in global and once per local space because uniqueness is:

```text
UNIQUE(canonical_key, scope_key)
```

A local variant must use the exact same `canonical_key` as its global equivalent.

## 6.4 Correction identity rule

When correcting a direct Q&A answer:

1. use `BotAnswer.qa_version_id`;
2. resolve the cited Q&A item;
3. reuse its canonical key;
4. global approval updates/creates the global item of that key;
5. local approval updates/creates the origin-space item of that key.

When correcting a synthesis answer with no cited Q&A item:

1. use the stored user question as `canonical_question`;
2. compute `canonical_key_for(question)`;
3. create/update the target scope item.

Do not perform an embedding search during approval to guess identity.

## 6.5 Current-version authority rule

Human-approved knowledge must not be silently replaced by an automated source refresh.

For `seed --renew`:

- if the current version is not human-approved, the new web version may become current;
- if the current version is human-approved, store the refreshed web version in history/provenance but keep the human-approved version current;
- expose the divergence in the human review report.

Do not implement “latest timestamp always wins”.

---

# 7. Search projection model

## 7.1 Stable vector IDs

Use exactly:

```text
Q&A vector:     qa:<qa_item_id>
message vector: msg:<message_id>
```

Never use a Q&A version id as a vector id after this refactor.

## 7.2 Required Q&A vector metadata

```json
{
  "kind": "qa",
  "object_id": "<qa_item_id>",
  "version_id": "<current_version_id>",
  "status": "active",
  "scope_key": "global|space:<id>",
  "canonical_key": "<semantic-key>",
  "authority": 100,
  "question": "...",
  "text": "...",
  "source_anchor": "...",
  "url": "...",
  "date": "...",
  "author": "..."
}
```

Keep metadata within provider limits; truncate display text if needed, but never truncate ids/keys.

## 7.3 Required message evidence metadata

```json
{
  "kind": "message_evidence",
  "object_id": "<message_id>",
  "scope_key": "global|space:<id>",
  "authority": 40,
  "text": "...",
  "question": "... or null",
  "author": "...",
  "date": "..."
}
```

Only evidence-eligible messages get a vector.

## 7.4 Projection manifest

Add a SQL table shared by local and production:

```sql
CREATE TABLE search_projection (
    vector_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

This table is derived operational state, not semantic truth.

Every successful vector upsert must upsert its manifest row.
Every successful vector delete must delete its manifest row.

## 7.5 Full rebuild semantics

A full rebuild does exactly:

1. list every current `search_projection.vector_id`;
2. delete those vectors from the vector store in bounded batches;
3. clear `search_projection`;
4. list current active Q&A items;
5. list only evidence-eligible messages;
6. embed and upsert in bounded batches;
7. write manifest rows only after successful vector upsert.

No rebuild may simply “upsert everything” without deleting the previous projection.

## 7.6 One-time legacy cleanup

Before the first v2 rebuild:

1. list every historical `qa_versions.id`;
2. list every existing `messages.id` that could have been indexed by legacy code;
3. call vector delete on those raw ids in bounded batches;
4. ignore missing-id errors;
5. then run the v2 rebuild.

Provide one explicit CLI operation:

```bash
kb index migrate-v2
```

It must require an explicit `--confirm` flag in Cloudflare mode.

Local mode may run it automatically on a disposable database.

## 7.7 Vectorize metadata indexes

Create and document metadata indexes for exactly:

```text
kind       string
status     string
scope_key  string
```

No deployment smoke test passes until a global scoped retrieval and a local scoped retrieval both return expected records.

---

# 8. Background conversation semantics

## 8.1 Store versus index

When `BACKGROUND_LISTENER_ENABLED=true`, persist every accepted background message and media metadata that passes channel access rules.

Do not use the classifier to decide whether the raw message exists in the database.

Classification controls only indexing/report semantics.

## 8.2 Deterministic prefilter

Before spending an embedding call, classify obvious non-evidence cases without AI.

At minimum:

- no text → `classification_status=no_text`, no AI, no vector;
- text shorter than 2 Unicode alphanumeric characters after stripping → chitchat, no AI;
- exact normalized acknowledgements in a small static set such as `ok`, `gràcies`, `gracias`, `perfecte`, `perfecto`, `d'acord` → chitchat, no AI;
- do not build a large NLP rules engine.

This prefilter is an optimization only. Keep it small and tested.

## 8.3 Classifier

Keep the embedding-prototype approach.

Use an enum, not arbitrary strings:

```python
class IntentLabel(StrEnum):
    QUESTION = "question"
    KNOWLEDGE_UPDATE = "knowledge_update"
    CORRECTION = "correction"
    CHITCHAT = "chitchat"
```

`MessageClassifier.classify()` must return:

```python
@dataclass(frozen=True, slots=True)
class Classification:
    scores: IntentScores
    best_label: IntentLabel
    best_score: float
    embedding: tuple[float, ...]
```

The returned message embedding is reused for indexing when possible.

Prototype vectors are cached per classifier instance, not in a module-global dictionary.

Dimension mismatch is an error. Use strict vector length validation; never silently truncate with `zip(..., strict=False)`.

## 8.4 What is evidence-eligible

### Never index as factual evidence

- standalone question;
- chitchat;
- ambiguous low-signal text;
- media without processed textual content;
- bot commands;
- the bot's own messages.

### Index as evidence

A standalone message may be indexed when:

```text
knowledge_update >= configured threshold
OR
correction >= configured threshold
```

A reply may be indexed as a Q→A pair when:

- parent message exists in the same space;
- parent classification is `question` above the question threshold;
- reply is answer-like (`knowledge_update` or `correction`) above answer threshold.

Embed the pair as:

```text
Question: <parent question>
Answer: <reply text>
```

Store the reply message id as the evidence object id.

## 8.5 Evidence authority

Compute message evidence authority in one pure domain policy. Do not infer it from a source-id string.

Use exactly:

```text
95  Telegram message authored by configured admin
50  WhatsApp import message
40  ordinary Telegram message
```

Q&A authority remains separate (`100` approved correction, `90` published web seed).

The D1/SQLite search-index query must join/read the source row's `source_type` and the message's `sender_is_admin` flag.

## 8.6 Immediate indexing

When an eligible background message is accepted:

- standalone update: reuse the classifier's message embedding;
- paired Q→A: issue one embedding for the combined pair;
- upsert immediately;
- update `messages.index_status='indexed'` only after vector upsert succeeds.

No routine full reindex is required.

## 8.7 Background AI budget

Background learning is lower priority than user questions.

Add:

```text
AI_BACKGROUND_BUDGET_FRACTION=0.50
AI_MAINTENANCE_BUDGET_FRACTION=0.70
```

Rules in Cloudflare mode:

- direct user question: never pre-rejected by the estimate guard;
- background classification/indexing: allowed only while estimated usage is below 50% of configured daily budget;
- manual maintenance/rebuild/smoke: allowed only below 70%, and only after explicit invocation;
- local mode: budget guard disabled.

When background work is denied by the guard:

- persist the message;
- set `classification_status='deferred_budget'`;
- do not call AI;
- include deferred count in the daily report;
- do not automatically catch up with an unbounded scheduled job.

Provide an explicit bounded maintenance command:

```bash
kb background process-backlog --limit 100
```

Cloudflare mode requires explicit human invocation.


---

# 9. Answering and retrieval — exact target behavior

The answering path must be intentionally small. It is not an agent.

## 9.1 Allowed answer modes

Keep exactly:

```python
class AnswerMode(StrEnum):
    DIRECT_QA = "direct_qa"
    SYNTHESIS = "synthesis"
    ABSTENTION = "abstention"
    UNAVAILABLE = "unavailable"
```

Do not add intermediate or speculative modes.

## 9.2 Retrieval inputs

`RetrievalService.retrieve()` receives:

```python
question: str
space_id: str | None
```

It does not receive a Telegram chat id.

When `space_id is None`, retrieval is global-only unless an explicitly documented internal/eval call requests all scopes. Normal user paths always resolve the space before retrieval.

## 9.3 One question embedding per answer request

Embed the cleaned user question exactly once.

Pass the resulting vector to all Q&A/message vector queries.

Do not call the embedder separately for global Q&A, local Q&A, and messages.

## 9.4 Q&A retrieval

When a space is present, perform two filtered vector queries:

```text
A. kind=qa, status=active, scope_key=global
B. kind=qa, status=active, scope_key=space:<space_id>
```

Use `qa_top_k=5` for each provider query unless settings override it.

Merge as follows:

1. sort local matches by similarity descending;
2. collect their `canonical_key` values;
3. remove global matches whose `canonical_key` is present locally;
4. concatenate local matches then remaining global matches;
5. sort the resulting candidates for final answer selection by:
   - similarity descending;
   - authority descending as tie breaker;
6. cap at `qa_top_k`.

The local variant suppresses only the global version of the **same canonical question**. It does not suppress unrelated global Q&A.

## 9.5 Message-evidence retrieval

Retrieve only `kind=message_evidence`.

For a space question, query:

```text
scope_key=space:<space_id>
```

Global message evidence may be included only when it was intentionally imported/created with `scope_key=global`; if supported, query it separately and merge after local evidence. Never expose another space's messages.

Default `message_top_k` after refactor: **4**.

The implementing agent MUST change the old default from 8 to 4 unless an existing regression test proves a product requirement for more.

## 9.6 Direct-answer decision

A direct answer is available when at least one active Q&A candidate has:

```text
similarity >= DIRECT_QA_THRESHOLD
```

Default remains `0.70` initially. Do not retune thresholds during structural refactor.

Choose the final direct candidate after local/global suppression and final sorting.

A direct answer:

- returns the stored Q&A text verbatim;
- does not call the generator;
- cites exactly that current Q&A version;
- persists `qa_version_id` on `BotAnswer`;
- persists the source item id inside structured source metadata.

## 9.7 Synthesis evidence gate

If there is no direct answer:

1. collect remaining evidence with similarity >= `SYNTHESIS_THRESHOLD`;
2. default threshold remains `0.30` until separately calibrated;
3. select at most 5 evidence records total;
4. selection order:
   - up to 2 Q&A records by similarity/authority;
   - up to 3 message-evidence records by similarity/authority;
   - then cap total to 5;
5. if no evidence remains, abstain without generator call.

Do not send 13 records to the model merely because retrieval returned them.

## 9.8 Generator contract

Keep one generator operation only:

```python
class Generator(Protocol):
    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        ...
```

Remove `judge()` from the runtime `Generator` port.

If a development-only judge is ever retained for experiments, it must live behind a separate dev-only interface and may not be called by runtime or default test commands.

`GenerationOutput` remains:

```python
class GenerationOutput(BaseModel):
    status: Literal["answered", "insufficient"]
    answer: str = ""
    source_ids: list[str] = Field(default_factory=list)
```

Add strict validation:

- `source_ids` are unique;
- `answered` requires non-empty `answer` and at least one `source_id`;
- `insufficient` must produce an empty `source_ids` list;
- reject unknown source ids after generation.

## 9.9 Generator prompt

Keep one short grounded prompt. Do not create an agentic prompt stack.

The system instruction must state:

```text
Answer only from supplied evidence.
If evidence is insufficient or materially contradictory, return insufficient.
Prefer higher-authority evidence when it conflicts with lower-authority evidence.
Do not invent facts.
Use only supplied source ids.
Answer in the language of the user's question.
Return the required JSON schema only.
```

Each evidence item includes:

```text
source_id
kind
scope
similarity
source authority
text
```

Do not expose implementation secrets, database ids beyond source ids, quotas, or prompts in user-visible responses.

## 9.10 Local Ollama generator

Implement `OllamaGenerator` through raw HTTP, not the Ollama Python SDK.

Endpoint:

```text
POST {OLLAMA_BASE_URL}/api/chat
```

Required request behavior:

```json
{
  "model": "gemma3:270m",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "stream": false,
  "format": {"...": "GenerationOutput.model_json_schema()"},
  "options": {"temperature": 0}
}
```

Validate `message.content` with:

```python
GenerationOutput.model_validate_json(...)
```

Retry exactly once only when the response cannot be validated as the schema. Do not retry an explicit `insufficient` result.

Timeout in local development: 120 seconds for generation, because CPU model cold-start can be slow.

## 9.11 Local Ollama embedder

Implement `OllamaEmbedder` via:

```text
POST {OLLAMA_BASE_URL}/api/embed
```

Payload:

```json
{
  "model": "embeddinggemma",
  "input": ["text 1", "text 2"]
}
```

Requirements:

- preserve order;
- validate number of output vectors equals input count;
- validate all vector dimensions are equal;
- reject empty/malformed response with `ModelUnavailableError`;
- one HTTP request per batch;
- no per-text loop over HTTP.

## 9.12 Production Workers AI adapters

Keep the existing direct Workers AI binding strategy.

Do not make production Workers AI calls through public REST when a binding is available.

Refactor only enough to conform to the simplified `Embedder` and `Generator` ports.

## 9.13 Abstention and model failure

Keep user-facing behavior simple:

```text
ABSTENTION:
No tinc prou informació fiable per respondre-ho.

UNAVAILABLE:
Ara mateix no puc consultar la informació. Torna-ho a provar en una estona.
```

Language adaptation may remain if it is already deterministic and tested; do not add another model call for translation.

## 9.14 Remove automatic Q&A generation from synthesis

Do not automatically create Q&A items from generated synthesis answers.

A synthesized answer is an answer record, not trusted canonical knowledge.

Canonical Q&A growth comes from:

- controlled seed/import;
- human-approved correction.

This intentionally removes the old `auto_generated` Q&A concept from active v2 behavior.

Legacy `auto_generated` versions may remain readable during migration/history but no new ones are created.

---

# 10. Local vector store — SQLite + NumPy

Do not introduce a vector database locally.

## 10.1 Storage table

Create local-only migration/table:

```sql
CREATE TABLE local_vectors (
    vector_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    status TEXT,
    canonical_key TEXT,
    object_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_local_vectors_kind_scope
    ON local_vectors(kind, scope_key);

CREATE INDEX idx_local_vectors_kind_status_scope
    ON local_vectors(kind, status, scope_key);
```

This table is a derived local search projection. It is safe to delete and rebuild.

It is not part of Cloudflare D1 migrations.

Put the DDL under:

```text
migrations/local/0001_local_vectors.sql
```

## 10.2 Storage format

Store embeddings as normalized NumPy `float32` bytes:

```python
array = np.asarray(values, dtype=np.float32)
array = array / np.linalg.norm(array)
blob = array.tobytes()
```

Reject a zero-norm vector.

On read:

```python
np.frombuffer(blob, dtype=np.float32, count=dimensions)
```

Do not pickle NumPy arrays.

## 10.3 Query implementation

`NumpySqliteVectorStore.query()`:

1. SQL-filter candidates by the supported equality filters (`kind`, `scope_key`, optional `status`);
2. load candidate vectors;
3. stack to a 2-D NumPy matrix;
4. normalize query vector;
5. compute cosine via matrix-vector dot product;
6. use `np.argpartition` or ordinary descending argsort; at this dataset size either is acceptable, but implement the clearer one first;
7. return top K matches in descending score order.

Use NumPy only here. Do not leak NumPy arrays through the `VectorStore` port.

## 10.4 Filter contract

The shared `VectorStore.query()` filter contract remains a small equality map.

Supported production/local keys after v2:

```text
kind
status
scope_key
```

If the local adapter receives an unsupported filter key, raise `ValueError`. Do not silently ignore it.

## 10.5 Rebuild

Local `clear()` may directly delete all rows from `local_vectors`.

Production Vectorize uses the manifest-driven delete behavior described earlier.

---

# 11. Data model changes and migrations

Do not drop existing production semantic/history tables before migration succeeds when an operator elects to preserve an existing D1 database. For this repository's production cutover, experimental D1 state is disposable and may be recreated from migrations plus committed `data/` seed material.

Create shared migration files beginning after the current highest migration. At review time the repository contains migrations through `0010_listener_intent.sql`; therefore use the following names unless another migration has landed before implementation starts. If numbering has changed, preserve the same logical sequence with the next available numbers.

```text
migrations/0011_spaces_channels.sql
migrations/0012_knowledge_identity.sql
migrations/0013_message_index_state.sql
migrations/0014_delivery_interactions.sql
migrations/0015_daily_report.sql
migrations/0016_search_projection.sql
```

## 11.1 `0011_spaces_channels.sql`

Create:

```sql
CREATE TABLE spaces (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE channel_bindings (
    channel TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    space_id TEXT NOT NULL REFERENCES spaces(id),
    title TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, external_conversation_id),
    UNIQUE (conversation_id)
);

CREATE INDEX idx_channel_bindings_space
    ON channel_bindings(space_id);
```

Add nullable `space_id` to existing `conversations` if not already represented through a binding. The final repository API must be able to resolve conversation → space without inspecting a Telegram id.

The exact migration may retain legacy conversation ids. Do not rewrite primary keys solely for cosmetic consistency.

### Legacy space backfill

Implement an idempotent maintenance service/CLI command:

```bash
kb maintenance backfill-spaces
```

For every existing Telegram group conversation without a binding:

```python
space_id = "sp_" + uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"knowledge-bot:telegram:{external_conversation_id}",
).hex
```

Create:

```text
space
channel_binding(channel="telegram", external_conversation_id=..., ...)
```

Use the existing conversation title when available.

Do not infer one space from another space by matching titles.

## 11.2 `0012_knowledge_identity.sql`

Add:

```text
qa_versions.source_anchor TEXT NULL
```

Rename conceptually, but not necessarily physically in the first SQL migration:

```text
qa_items.scope -> scope_key
sources.scope  -> scope_key
```

The target physical column name is `scope_key`. First attempt a migration using SQLite/D1-supported `ALTER TABLE ... RENAME COLUMN scope TO scope_key` and verify it in both temporary SQLite and the Cloudflare-compatible migration smoke environment. If the installed D1 compatibility level rejects that exact operation, use the established child-first table-rebuild technique from `0008_scope.sql`. Do not leave the final schema with mixed `scope`/`scope_key` names.

### Backfill web anchors before canonical key migration

For legacy web Q&A versions whose item key contains the source anchor:

```text
qav.source_anchor = old qa_item.canonical_key
```

Only do this for legacy web-seeded versions where the anchor is not already populated.

### Canonical identity normalization command

Implement:

```bash
kb maintenance normalize-knowledge-identity
```

Algorithm, exactly:

1. load all Q&A items and all their versions;
2. compute `new_key = canonical_key_for(item.canonical_question)`;
3. group by `(scope_key, new_key)`;
4. for each group:
   - choose survivor item using the current version with highest authority;
   - tie break by current-version `created_at` descending;
   - final tie break by item id ascending;
5. move every duplicate item's versions to the survivor item;
6. update feedback `qa_id` references from duplicate to survivor;
7. select the survivor current version using the same authority/date rule;
8. delete duplicate item rows only after references have moved;
9. update survivor `canonical_key = new_key`;
10. preserve every historical version/evidence row;
11. report counts: items scanned, groups merged, versions moved, feedback refs changed.

Because the existing unique key can conflict during rewriting, perform a temporary-key phase inside the maintenance operation:

```text
legacy:<qa_item_id>
```

before assigning final semantic keys.

This maintenance operation must be tested with a fixture containing:

- one web item keyed by anchor;
- one duplicate correction item keyed by old question hash;
- different current authorities;
- feedback pointing at duplicate;
- evidence rows for both.

Expected result: one item, all versions preserved, highest-authority current version selected, feedback repaired.

After normalization, enforce:

```text
UNIQUE(canonical_key, scope_key)
```

If the existing table must be rebuilt to change the unique constraint/column name, use the established D1 child-first table rebuild pattern from migration `0008_scope.sql`.

## 11.3 Scope backfill

After spaces exist, convert any legacy Q&A/source scope equal to a Telegram external chat id into:

```text
space:<resolved-space-id>
```

Global remains exactly `global`.

Unknown non-global legacy scope is a migration error. Do not silently create an unbound scope.

## 11.4 `0013_message_index_state.sql`

Add to `messages`:

```text
classification_status TEXT
intent_scores_json TEXT
index_status TEXT NOT NULL DEFAULT 'not_indexed'
indexed_at TEXT
```

Keep existing:

```text
intent_label
intent_score
context_question
```

Allowed `classification_status` values in v2:

```text
not_classified
no_text
prefilter_chitchat
classified
deferred_budget
failed
```

Allowed `index_status` values:

```text
not_indexed
not_eligible
pending
indexed
failed
```

Do not add more states without a product reason.

## 11.5 `0014_delivery_interactions.sql`

Create generic delivery receipts:

```sql
CREATE TABLE delivery_receipts (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (object_type, object_id, channel)
);
```

Create Telegram-specific adapter correlation table:

```sql
CREATE TABLE telegram_interactions (
    external_message_id TEXT PRIMARY KEY,
    interaction_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    principal_id TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
```

Allowed interaction types initially:

```text
feedback_proposal
review_edit
```

Callback buttons do not need a stored interaction row because they carry an object id and action; authorization still happens server-side.

Move legacy `proposal_prompt_message_id` / `edit_prompt_message_id` usage out of application logic. Columns may remain for one migration cycle but must not be read by v2 code.

## 11.6 Reviewer representation

Migrate reviewer user identity from raw Telegram id to `principal_id`.

Final reviewer record:

```text
scope_key
principal_id
display_name
nominated_by_principal_id
created_at
```

For legacy rows:

```text
principal_id = "telegram:" + old_user_id
nominated_by_principal_id = "telegram:" + old_nominated_by
```

Legacy local reviewer scope equal to Telegram chat id must be converted to `space:<space_id>`.

## 11.7 `0015_daily_report.sql`

Create:

```sql
CREATE TABLE daily_report_state (
    key TEXT PRIMARY KEY,
    last_sent_at TEXT
);
```

Use one key:

```text
admin
```

Existing recap/report state may remain for migration safety but v2 application code must no longer use opportunistic recap/report state.

## 11.8 `0016_search_projection.sql`

Create the shared `search_projection` table defined in Section 7.

## 11.9 Sources

Preserve the existing source table concept but enforce these final semantics:

```text
id              unique source-instance id
source_type     closed SourceType value
external_ref    URL/import fingerprint/channel descriptor as appropriate
title
canonical_url
authority       base source authority
is_mutable
created_at
scope_key
```

Backfill legacy ids only when required for collision-free provenance. New ingestion code must never assume `id == source_type`.

When building message evidence, load `source_type`/base authority from the source row and apply the admin-author override policy in the application/domain layer.

## 11.10 Bot answers

Ensure `bot_answers` has/retains:

```text
id
conversation_id
space_id
user_message_id
question
answer
answer_mode
qa_version_id
sources_json
created_at
```

Add `space_id` if absent.

`sources_json` becomes a JSON array of structured source references rather than only source-id strings for new v2 answers. Keep parsing of legacy string arrays only inside the repository migration/compatibility read path; do not spread legacy branching through application logic.

## 11.11 Feedback

`feedback.qa_id` has one invariant in v2:

```text
it is a QA ITEM id, never a version id.
```

At feedback start:

- if `BotAnswer.qa_version_id` exists, resolve it to its QA item and store the item id;
- otherwise `qa_id=NULL` until approval resolves/creates the target item.

Add if needed:

```text
origin_space_id TEXT
reporter_principal_id TEXT
```

The reporter delivery target is adapter data, not reviewer authorization data.

---

# 12. Transaction boundaries

The refactor must fix multi-write partial-state risks without introducing a generic Unit of Work framework.

## 12.1 Approved correction commit

Introduce one purpose-specific port:

```python
class CorrectionCommitStore(Protocol):
    async def approve(self, command: ApproveCorrectionCommand) -> ApprovedCorrection:
        ...

    async def reject(self, command: RejectCorrectionCommand) -> Feedback:
        ...
```

The D1 implementation uses `DB.batch(...)` so these writes are one transaction:

- create target QA item if necessary;
- insert Q&A version;
- update Q&A current pointer;
- insert Q&A evidence link;
- update feedback status/resolved timestamp.

The SQLite implementation uses one `BEGIN`/commit transaction through `aiosqlite`.

Do not coordinate these five writes from FastAPI routes.

## 12.2 Seed version update

Likewise, put item/version pointer changes behind one storage operation so the database cannot point to a version that failed to insert.

A generic transaction abstraction is forbidden. Use narrowly named persistence operations matching the domain transaction.

## 12.3 Search projection is post-commit

Vector index updates occur **after** the SQL semantic transaction commits.

If vector update fails:

- semantic SQL state remains correct;
- mark projection/index status as failed/pending;
- return a result indicating knowledge was committed but search projection needs retry;
- do not roll back approved knowledge because Vectorize/Ollama failed.

Provide a bounded repair command:

```bash
kb index repair --limit 100
```

---

# 13. Reviewer and correction workflows

## 13.1 Application services

Split the current large feedback/router behavior into these explicit application responsibilities:

```text
CorrectionService
ReviewerService
ReviewAuthorization
```

Do not create more layers than these.

### `CorrectionService`

Methods:

```python
async def start(answer_id, reporter_principal_id, reporter_name) -> CorrectionStart
async def submit_proposal(feedback_id, reporter_principal_id, text) -> ReviewTask
async def update_draft(feedback_id, actor_principal_id, text) -> ReviewTask
async def decide(feedback_id, actor_principal_id, decision) -> ReviewDecisionResult
```

`CorrectionService` contains no Telegram rendering.

### `ReviewerService`

Methods:

```python
async def assign(scope_key, principal_id, display_name, nominated_by) -> Reviewer
async def remove(scope_key, actor_principal_id) -> bool
async def list() -> list[Reviewer]
async def destination(origin_space_id) -> ReviewerDestination
```

Only admin may assign/remove. Admin authorization is checked in application service or a shared policy, not only in Telegram route.

### `ReviewAuthorization`

Pure deterministic policy. Given:

```text
actor_principal_id
admin_principal_id
origin_scope_key
assigned local reviewer
assigned global reviewer
requested action
requested approval scope
```

returns allowed/denied.

This policy must be extensively unit tested.

## 13.2 Review routing

For a correction originating in a space:

```text
local reviewer for origin space
    else global reviewer
    else admin
```

For correction not tied to a space:

```text
global reviewer
    else admin
```

If the selected reviewer cannot receive a private Telegram message, keep the review task pending, notify the admin, and mention the reviewer in the origin group with a privacy-safe request to open a private chat with the bot. Do not silently route the decision to another reviewer. A later retry may deliver the task after the reviewer has activated the bot.

## 13.3 Allowed review actions

Use an enum:

```python
class ReviewAction(StrEnum):
    APPROVE_LOCAL = "approve_local"
    APPROVE_GLOBAL = "approve_global"
    EDIT = "edit"
    REJECT = "reject"
```

Do not pass unvalidated string actions through application services.

## 13.4 State transition rules

Feedback transitions:

```text
AWAITING_PROPOSAL
  -> PENDING_REVIEW

PENDING_REVIEW
  -> PENDING_REVIEW    # edit draft
  -> APPROVED
  -> REJECTED
```

Once `APPROVED` or `REJECTED`:

- another approve/reject/edit call returns a domain conflict;
- no new Q&A version is created;
- HTTP returns 409;
- Telegram callback is acknowledged with a short “already resolved” message.

This fixes repeat-callback duplicate approval risk.

Rename `PENDING_ADMIN` to `PENDING_REVIEW` in v2 domain semantics. Migrate stored value or map legacy reads at the repository boundary.

## 13.5 Approval target

`APPROVE_LOCAL` always targets:

```text
space:<origin_space_id>
```

No request may provide an arbitrary local space id.

`APPROVE_GLOBAL` always targets `global`.

## 13.6 Attribution

For an approved correction:

- preserve the correction proposer as the original author when known;
- record separately whether the text was edited by a reviewer and which reviewer approved it;
- render provenance truthfully as proposed-by, edited-by when applicable, and approved-by;
- do not attribute reviewer-written text to the original proposer without also exposing the edit.

## 13.7 Revert

Keep revert admin-only and operational.

A revert:

1. validates current version has a valid superseded version;
2. atomically updates current pointer;
3. commits SQL;
4. reindexes the stable `qa:<qa_item_id>` vector;
5. records an audit/review event.

No Telegram rollback button.

---

# 14. Generic REST boundary

The generic REST API is the canonical application boundary for future Web/WhatsApp integrations.

The co-deployed Telegram webhook may call the same application services in-process; it MUST use the same Pydantic request/result contracts where practical. Do not make the Worker call itself over HTTP.

All REST endpoints are stateless between calls.

## 14.1 REST/authentication rule

All externally invoked channel traffic reaches the service over HTTP/REST: Telegram webhook, future WhatsApp webhook, future Web application calls, operational calls, and local Ollama inference. The co-deployed Telegram route MUST NOT make a wasteful HTTP call back into the same process; it translates the REST webhook into the same Pydantic application contracts and calls the use case in-process. This is still one stateless REST service, not two services.

Until a real Web/WhatsApp authentication layer exists, **every generic `/v1/*` endpoint requires `X-Internal-Key`**. Telegram does not call those endpoints over the network; its webhook is authenticated by Telegram webhook secret and calls the same use cases in-process. A future Web/WhatsApp adapter must authenticate users/channel requests before invoking the generic API.

Never accept a caller-supplied `principal_id` on an unauthenticated public endpoint.

## 14.2 Route modules

Replace the monolithic `fastapi_routes.py` with exactly:

```text
src/knowledge_bot/adapters/http/app.py
src/knowledge_bot/adapters/http/api_routes.py
src/knowledge_bot/adapters/http/internal_routes.py
src/knowledge_bot/adapters/telegram/routes.py
```

Do not create one file per endpoint.

## 14.3 Generic application endpoints

### `POST /v1/questions`

Request:

```python
class AskQuestionRequest(BaseModel):
    request_id: str
    space_id: str | None = None
    principal_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)
```

Response:

```python
class AnswerSource(BaseModel):
    source_id: str
    kind: Literal["qa", "message"]
    label: str
    url: str | None = None
    author: str | None = None
    date: str | None = None

class AskQuestionResponse(BaseModel):
    answer_id: str
    mode: AnswerMode
    answer: str
    rendered_text: str
    sources: list[AnswerSource]
    can_report: bool
```

`request_id` is the durable idempotency key for the question request. Persist it with the answer under a uniqueness constraint. A repeated request with the same `request_id` returns the existing `AskQuestionResponse` without retrieval, generation, or a second answer row. Reusing one `request_id` with a different normalized question is a conflict.

This route performs no Telegram delivery.

### `POST /v1/feedback`

Request:

```python
class StartFeedbackRequest(BaseModel):
    answer_id: str
    reporter_principal_id: str
    reporter_name: str | None = None
```

Response includes:

```text
feedback_id
question
current_answer
status
```

### `PUT /v1/feedback/{feedback_id}/proposal`

Request:

```text
reporter_principal_id
proposal
```

Return a structured `ReviewTask`.

### `PUT /v1/reviews/{feedback_id}/draft`

Request:

```text
actor_principal_id
text
```

Authorization required.

### `POST /v1/reviews/{feedback_id}/decision`

Request:

```python
class ReviewDecisionRequest(BaseModel):
    actor_principal_id: str
    action: Literal["approve_local", "approve_global", "reject"]
```

Do not use booleans such as `global=true`.

Return:

```text
status
feedback_id
qa_item_id (if approved)
qa_version_id (if approved)
projection_status
```

## 14.4 Reviewer admin endpoints

Require internal/admin authentication.

```text
GET    /internal/reviewers
PUT    /internal/reviewers/{scope_key}
DELETE /internal/reviewers/{scope_key}
```

Assignment body:

```text
principal_id
display_name
actor_principal_id
```

The application still verifies actor is admin.

## 14.5 Operational endpoints

Keep a small authenticated operational API:

```text
POST /internal/seed
POST /internal/index/rebuild
POST /internal/index/repair
POST /internal/index/migrate-v2
POST /internal/background/process-backlog
POST /internal/jobs/daily-report
POST /internal/maintenance/backfill-spaces
POST /internal/maintenance/normalize-knowledge-identity
POST /internal/revert
GET  /internal/review
GET  /healthz
GET  /readyz
```

Remove or deprecate the generic quota-burning live-eval endpoints from normal operations.

If temporary eval endpoints are retained, place them under `/internal/dev/` and compile/document them as non-routine. They must still be budget guarded.

## 14.6 Validation

Every JSON route body is a Pydantic model parameter. Do not manually call `request.json()` and then cast arbitrary fields in normal endpoints.

FastAPI/Pydantic validation errors should naturally return 422 unless there is a product reason for a different 4xx.

## 14.7 Error mapping

Map domain/application errors centrally:

```text
not found          -> 404
unauthorized       -> 403
invalid transition -> 409
invalid data       -> 422
model unavailable  -> 503 only for direct REST; Telegram maps to user-friendly text
budget denied      -> 429 for manual maintenance only
```

Do not expose raw exception text to users.


## 14.8 Canonical CLI surface

Keep one Typer application, `kb`. Do not create parallel ad-hoc scripts for normal operations.

The final v2 CLI exposes these canonical command families:

```text
kb space add --title <title>
kb space list
kb channel bind telegram --space-id <space_id> --external-conversation-id <chat_id> [--title <title>]
kb channel list
kb seed --qa <file> [--space-id <space_id>] [--renew]
kb seed --messages <file> [--space-id <space_id>]
kb index rebuild
kb index repair --limit 100
kb index migrate-v2 --confirm
kb background process-backlog --limit 100
kb maintenance backfill-spaces
kb maintenance normalize-knowledge-identity
kb reviewer list
kb revert <qa_item_id>
kb telegram set-webhook --base-url <https-url>
kb telegram delete-webhook
```

Rules:

- seed without `--space-id` is global;
- never accept a raw Telegram chat id as `--scope`; bind the chat to a space first;
- Cloudflare-mutating/index commands use the authenticated REST operational endpoints rather than duplicating application logic in the CLI;
- local commands may call the local REST API or the same application services through a small local command adapter, but behavior must be identical;
- no CLI command silently performs a full remote reindex.

---

# 15. Telegram adapter — exact responsibilities

Telegram remains the only runtime channel in v2, but must be replaceable.

## 15.1 Inbound responsibilities

The Telegram adapter owns all Telegram-specific knowledge of external identities,
source ids, chat bindings, and principal-to-chat routing. It passes opaque
channel/external values and resolved `space_id`/`principal_id` values to the
application. The application-level space/binding service MUST NOT import Telegram
modules or contain Telegram-specific branches.

Telegram adapter may:

- validate webhook secret;
- parse Telegram Update Pydantic contracts;
- resolve Telegram group/private chat;
- convert Telegram user id to `principal_id`;
- detect whether the bot was addressed;
- parse Telegram-only `/reviewer`, `/ask`, `/whoami`, `/chatid` conveniences;
- resolve `(telegram, chat_id)` to a space/channel binding; an unbound group is not served;
- translate Telegram reply/callback mechanics to generic feedback/review calls.

It may not:

- perform retrieval itself;
- touch repositories through service internals;
- decide knowledge identity;
- update Q&A tables;
- decide reviewer permissions;
- call Vectorize directly.

## 15.2 Outbound client

Rename the low-level outbound adapter concept to `TelegramClient` or equivalent. It may expose Telegram API primitives:

```text
send_text
send_buttons
send_force_reply
edit_text
answer_callback
```

Those methods are allowed because this class is explicitly Telegram infrastructure.

Do not expose those methods through a core/application port named generically `MessageTransport`.

## 15.3 Answer delivery

On an addressed Telegram message:

1. persist/resolve normalized inbound message;
2. ask application service for `AskQuestionResponse`;
3. check `delivery_receipts` for `(object_type='answer', answer_id, channel='telegram')`;
4. if already delivered, do not send again;
5. otherwise render Telegram answer + “Està malament?” button;
6. send;
7. persist delivery receipt after successful Telegram API response.

If application result exists but prior delivery failed, a retried Telegram webhook is allowed to attempt delivery again.

Telegram API response with `ok=false` is a failure even if HTTP status is 200. Fix the current adapter behavior accordingly.

## 15.4 Feedback start

Callback:

```text
feedback:start:<answer_id>
```

Flow:

1. map callback actor to `principal_id`;
2. call `CorrectionService.start()`;
3. DM reporter with ForceReply;
4. persist `telegram_interactions` row of type `feedback_proposal` keyed by the sent prompt message id;
5. acknowledge callback.

If DM fails because the user has never opened the bot:

- acknowledge callback with human-friendly alert;
- optionally send existing group notice with bot link;
- do not corrupt feedback state;
- a later retry may resume/start safely.

## 15.5 Proposal reply

When a private Telegram message replies to another message:

1. look up `telegram_interactions` by replied-to external message id;
2. if type `feedback_proposal`, ensure not consumed;
3. call generic proposal endpoint/service with stored feedback id and current principal;
4. mark interaction consumed;
5. acknowledge reporter;
6. route review task to reviewer destination;
7. render only actions allowed for that reviewer.

## 15.6 Review buttons

### Local reviewer buttons

Render:

```text
[👥 Aprovar grup] [✏️ Editar] [❌ Rebutjar]
```

Do not render global approval.

### Global reviewer/admin buttons

Render:

```text
[🌐 Aprovar global] [👥 Aprovar grup] [✏️ Editar] [❌ Rebutjar]
```

Server-side policy remains authoritative even if a button is forged.

## 15.7 Review edit

`✏️ Editar`:

1. authorization check;
2. send ForceReply containing current proposal;
3. store `telegram_interactions(type='review_edit')`;
4. when reply arrives, authorization check again;
5. update draft only;
6. render review buttons again;
7. do not auto-approve the edit.

## 15.8 Reviewer commands

Keep Telegram commands as adapter conveniences but call generic reviewer application service.

Admin-only:

```text
/reviewer              list
/reviewer              while replying -> assign local reviewer for current space
/reviewer global       while replying -> assign global reviewer
/reviewer off          remove local reviewer
/reviewer off global   remove global reviewer
```

Safe command parsing:

```python
text = (message.text or "").strip()
if not text:
    ...
first, *rest = text.split()
```

Never index `split()[0]` without checking.

## 15.9 Telegram adapter must not own application state

No Python dict may track pending feedback/review replies across requests.

All correlation is in SQL.

---

# 16. Daily report and scheduling

Remove opportunistic recap/report calls from inbound message handling.

## 16.1 One report service

Replace `RecapService` + batch reviewer report scheduling with one `DailyReportService`.

It is deterministic and performs no AI calls.

## 16.2 Report window

Default window:

```text
[last_successful_report_at, now)
```

If no previous report exists:

```text
[now - 24 hours, now)
```

Store `last_sent_at` only after successful admin delivery.

## 16.3 Report content

Include concise sections:

1. **Addressed questions**
   - total;
   - direct;
   - synthesis;
   - abstention;
   - unavailable;
   - number flagged wrong.
2. **Background questions detected**
   - group/space label;
   - count;
   - whether a later paired answer was captured;
   - no raw question text.
3. **Background knowledge ingestion**
   - messages stored;
   - evidence records indexed;
   - chitchat/non-evidence;
   - deferred due to AI budget;
   - failures.
4. **Corrections/reviews**
   - proposed;
   - approved local;
   - approved global;
   - rejected;
   - reviewer name/space for audit.
5. **Seed divergence**
   - refreshed web versions that did not replace a human-approved current version;
   - affected Q&A item/version references;
   - no raw knowledge text.
6. **Estimated AI usage**
   - calls;
   - estimated neurons;
   - configured daily budget.

The report must not ask an LLM to summarize any of these sections.

## 16.4 Cloudflare schedule

Configure exactly one daily Cron Trigger initially:

```text
0 19 * * *
```

Cron is UTC. This runs at approximately 20:00 CET / 21:00 CEST in Barcelona; document this rather than pretending the cron itself is Europe/Madrid aware.

If the human later wants another time, change only the cron configuration.

## 16.5 Python Worker entrypoint

Replace simple `asgi.entrypoint(app)` usage with an explicit `WorkerEntrypoint` so fetch and scheduled coexist.

Target shape:

```python
from workers import WorkerEntrypoint, asgi

class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, self.env)

    async def scheduled(self, controller, env, ctx):
        if controller.cron == "0 19 * * *":
            await run_daily_report_from_worker_env(self.env)
```

Use the exact API supported by the installed Workers SDK at implementation time; do not keep the old claim that Python Workers cannot schedule.

## 16.6 Manual job endpoint

`POST /internal/jobs/daily-report` must call the same application job logic.

This is the canonical way to test locally and operationally.

Do not duplicate report construction in the scheduled handler.

---

# 17. Fully local Docker development environment

Local development is the default development mode after this refactor.

An agent should be able to implement most of the project without Cloudflare credentials and without consuming a single Workers AI neuron.

## 17.1 No Docker Compose

Preserve the repository rule: do not add Docker Compose.

The Makefile/scripts manage two containers on one named Docker network.

## 17.2 Docker targets

Convert the Dockerfile to a multi-stage development image with at least:

```text
base
local
cloudflare-dev
```

### `base`

Contains:

- Python 3.13;
- `uv`;
- project source/dependencies common to both profiles;
- non-root user if it does not break bind mounts.

### `local`

Adds local dependency groups:

```text
numpy
aiosqlite
httpx
uvicorn
Typer/ops tools as needed
```

No Node or Wrangler.

Default command:

```text
uv run uvicorn local_entry:app --host 0.0.0.0 --port 8000
```

### `cloudflare-dev`

Adds:

- Node compatible with current Wrangler;
- Wrangler/pywrangler requirements;
- Workers Python SDK dev dependencies.

Used only for Cloudflare compatibility smoke/deploy tasks.

## 17.3 Docker network/volumes

Canonical names:

```text
network: knowledge-bot-dev
app container: knowledge-bot-local
ollama container: knowledge-bot-ollama
SQLite volume: knowledge-bot-data
Ollama model volume: knowledge-bot-ollama-data
```

Do not invent alternate names in scripts/docs.

## 17.4 Canonical local environment variables

Provide `.env.local.example` with:

```dotenv
KB_RUNTIME=local
KB_SQLITE_PATH=/data/knowledge-bot.sqlite3
OLLAMA_BASE_URL=http://knowledge-bot-ollama:11434
EMBEDDING_MODEL=embeddinggemma
GENERATION_MODEL=gemma3:270m
BACKGROUND_LISTENER_ENABLED=true
DIRECT_QA_THRESHOLD=0.70
SYNTHESIS_THRESHOLD=0.30
QA_TOP_K=5
MESSAGE_TOP_K=4
LOG_LEVEL=DEBUG
KB_LOG_CONTENT=false
INTERNAL_ADMIN_KEY=local-development-key
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_BOT_ID=
TELEGRAM_BOT_USERNAME=
ADMIN_TELEGRAM_USER_ID=
```

No Cloudflare token appears in this file.

Do not keep `ALLOWED_TELEGRAM_CHAT_IDS` as a second source of group authorization in v2. A Telegram group is served if and only if it has an active `channel_bindings` row. The migration/backfill command may read the legacy environment allow-list once to help create bindings, but final request authorization uses the binding table.

## 17.5 Canonical Makefile commands

Implement these exact user-facing commands:

```text
make dev-bootstrap
make dev-up
make dev-down
make dev-logs
make dev-shell
make dev-reset
make dev-migrate
make dev-seed
make test
make test-integration
make test-e2e-local
make smoke-cloudflare
```

### `make dev-bootstrap`

Idempotently:

1. create Docker network;
2. create volumes;
3. start Ollama container if absent;
4. wait for Ollama HTTP readiness;
5. pull `embeddinggemma` if missing;
6. pull `gemma3:270m` if missing;
7. build local app image;
8. apply shared + local migrations;
9. leave services ready or call `dev-up`.

It must not contact Cloudflare.

### `make dev-up`

Start/reuse Ollama and app containers.

Expose:

```text
localhost:8000
```

### `make dev-down`

Stop/remove containers but preserve volumes.

### `make dev-reset`

Explicitly destructive. Require:

```text
CONFIRM=1
```

Example:

```bash
make dev-reset CONFIRM=1
```

It removes only the local SQLite volume/state, not Ollama model volume unless separately requested.

### `make dev-logs`

Follow app logs. A separate target may show Ollama logs, but `dev-logs` defaults to app.

## 17.6 Ollama image pinning

Do not add an Ollama Python dependency.

The implementation PR must pin the Ollama Docker image to a concrete version or digest that is verified to support `embeddinggemma` (Ollama documentation currently requires v0.11.10 or newer for this model).

The coding agent may resolve the concrete current stable image during implementation, but once selected it must be pinned in one place (Makefile/script variable) and documented. Do not scatter `latest` in multiple files.

## 17.7 Ollama readiness

Readiness is not a generation call.

Use a lightweight Ollama API check such as listing models/server status.

Model pull/warm-up is setup behavior; ordinary `make test` must not start or call Ollama.

## 17.8 Local app readiness

`GET /healthz`:

```json
{"status":"ok"}
```

No dependencies checked.

`GET /readyz` in local mode checks:

- SQLite connection and simple `SELECT 1`;
- local vector schema exists;
- Ollama server reachable without running inference;
- required model names present in Ollama model list.

It must not consume generation/embedding work.


## 17.9 Local migration runner

Local development must not reapply raw SQL blindly on every start. Implement one tiny local migration runner; do not add Alembic or another migration framework.

Create a local bookkeeping table:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

`make dev-migrate` must:

1. open the configured SQLite database;
2. enable foreign keys;
3. list `migrations/[0-9]*.sql` in lexical order;
4. apply only files absent from `schema_migrations`;
5. execute each migration file as one transaction;
6. record the filename only after successful commit;
7. then apply `migrations/local/[0-9]*.sql` with the same mechanism;
8. stop immediately on first failure.

The local runner is only for SQLite development. Production D1 migrations continue through the explicitly documented Wrangler/D1 path; do not invent a remote migration ORM.

---

# 18. Local settings and composition

## 18.1 Runtime enum

Define:

```python
class RuntimeMode(StrEnum):
    LOCAL = "local"
    CLOUDFLARE = "cloudflare"
```

No auto-detection.

## 18.2 Settings parsing

Pydantic is the single parser/validator.

Remove manual helpers that silently coerce invalid values to defaults, such as `_int()` / `_float()` fallback behavior.

Invalid configured values fail startup with a clear validation error.

Validate at minimum:

```text
thresholds in [0,1]
top_k >= 1
intervals > 0
budget > 0
budget fractions in (0,1)
background fraction < maintenance fraction < 1
required model names non-empty
```

## 18.3 Local composition root

Create:

```text
src/knowledge_bot/infrastructure/local/composition.py
```

It creates one shared instance per repository/store and reuses it throughout the graph.

Do not instantiate separate SQLite repository objects with unrelated connections/state for services that should share a backend transaction context.

The local graph uses:

```text
SystemClock
SQLite repositories
NumpySqliteVectorStore
HttpxClient
OllamaEmbedder
OllamaGenerator
No-op/unlimited local AiBudget policy
TelegramClient only if Telegram credentials are configured
```

## 18.4 Cloudflare composition root

Create:

```text
src/knowledge_bot/infrastructure/cloudflare/composition.py
```

It uses:

```text
D1 repositories
VectorizeStore
WorkersAIEmbedder
WorkersAIGenerator
Metered wrappers
TelegramClient with Workers HTTP adapter
```

Instantiate each repository class once per composition root where practical and reuse it.

Do not expose raw repositories in `AppContext` merely so routes can reach around services.

## 18.5 Context exposed to routes

The HTTP/Telegram layer should receive a compact services object such as:

```text
question_service
message_service
correction_service
reviewer_service
daily_report_job
index_service
seed_service
maintenance_service
telegram_client (Telegram routes only)
settings
```

No `feedback_repo` field in route context.
No `answer.retrieval` access from route code.

---

# 19. Local/production persistence interface

Do not build a generic ORM.

Keep repository protocols purpose-based.

## 19.1 Shared behavior contract tests

For each important repository behavior, write contract tests that run against:

- in-memory fake where appropriate;
- SQLite adapter in normal CI/integration tests;
- D1 only in explicit Cloudflare smoke tests where necessary.

The SQLite adapter should be considered the canonical executable local persistence implementation.

## 19.2 SQLite connection rules

Use `aiosqlite`.

- enable foreign keys on each connection;
- use parameterized SQL only;
- no string interpolation for values;
- repository methods remain async;
- transactions are explicit in purpose-specific atomic operations.

## 19.3 D1 rules

- prepared statements for values;
- `DB.batch()` for purpose-specific atomic multi-write transactions;
- no SQL in application use cases;
- do not swallow D1 errors at repository layer.

---

# 20. Logging and debugging

The local-first environment must make functional debugging easy without making production logs unsafe.

## 20.1 Structured event names

Log events, not prose blobs. Examples:

```text
inbound_received
message_persisted
background_prefiltered
message_classified
message_indexed
question_retrieved
answer_direct
answer_synthesis
answer_abstained
model_unavailable
feedback_started
proposal_submitted
review_authorized
review_denied
correction_committed
projection_failed
daily_report_sent
```

## 20.2 Required fields where relevant

```text
request_id
channel
conversation_id
space_id
message_id
answer_id
feedback_id
qa_item_id
qa_version_id
scope_key
intent_label
intent_score
retrieval_count
retrieval_top_score
answer_mode
model
elapsed_ms
```

## 20.3 PII/content rule

Production/cloudflare mode never logs:

```text
raw message text
raw answers
names
phone numbers
usernames
full prompts
raw Telegram payloads
bot token
webhook secret
```

Local mode may support:

```text
KB_LOG_CONTENT=true
```

but Settings MUST reject `KB_LOG_CONTENT=true` when `KB_RUNTIME=cloudflare`.

Default is false everywhere.

## 20.4 Exception logging

At external boundaries, log exceptions with ids/metadata but not secret/raw content.

Do not catch broad `Exception` in application/domain code.

Broad exception translation is allowed at an external provider boundary when converted to a specific domain/infrastructure error and logged safely.

---

# 21. AI metering and quota safety

## 21.1 Local mode

Do not meter “neurons”. Local mode may record call counters/timing for diagnostics but never block work because of quota.

## 21.2 Cloudflare mode

Keep conservative estimated metering because actual account usage is not synchronously available to the Worker.

Classify work into:

```python
class AiWorkClass(StrEnum):
    USER = "user"
    BACKGROUND = "background"
    MAINTENANCE = "maintenance"
```

Policy:

```text
USER:        attempt normally
BACKGROUND:  refuse before call above background fraction
MAINTENANCE: refuse before call above maintenance fraction
```

## 21.3 No test should accidentally consume production quota

Hard rules:

- `make test` never makes network calls;
- `make test-integration` never makes network calls;
- `make test-e2e-local` calls only local Ollama;
- CI never calls Workers AI;
- no test auto-runs `reindex` against Cloudflare;
- no failed live test is automatically retried;
- agent instructions explicitly forbid cloud AI invocation without human authorization.

---

# 22. Test strategy

## 22.1 Tier 1 — unit

Directory:

```text
tests/unit/
```

No I/O.

Must cover:

- scope parsing;
- canonical normalization/key;
- review authorization matrix;
- intake decisions;
- classifier threshold policy with fixed vectors;
- evidence eligibility policy;
- local/global Q&A merge;
- answer gate;
- report rendering;
- settings validation.

## 22.2 Tier 2 — architecture

Directory:

```text
tests/architecture/
```

Keep AST dependency checks and add rules that detect:

- `domain` importing framework/infrastructure;
- `application` importing adapters/infrastructure/FastAPI/Workers/Telegram;
- HTTP/Telegram routes importing concrete D1/Vectorize/Workers AI classes;
- application imports of Telegram contract modules;
- direct `datetime.now` in application modules;
- route access to known repository fields on context.

Do not attempt a full static architecture framework.

## 22.3 Tier 3 — application integration with one shared backend

Directory:

```text
tests/integration/
```

Create one `InMemoryBackend` fixture that owns:

```text
sources
spaces
channel bindings
conversations
messages
answers
qa items
qa versions
qa evidence
feedback
reviewers
review events
delivery receipts
interaction state
projection manifest
AI usage/report state
```

All services in a test context receive repositories backed by this same object.

Delete the current pattern where each service may receive a separately constructed in-memory repository.

## 22.4 Mandatory domain E2E integration scenario

Create one named test that exercises the core semantics without real AI:

```text
test_global_local_review_lifecycle_survives_restart_semantics
```

Scenario:

1. seed global Q&A `Q -> Global V1`;
2. ask from space A → `Global V1` direct;
3. assert answer stores cited `qa_version_id`;
4. start correction from that answer;
5. local reviewer for A proposes/approves local correction → `Local A V1`;
6. ask from A → `Local A V1`;
7. ask from B → `Global V1`;
8. local reviewer attempts global approval via direct service call → denied;
9. global reviewer approves new global `Global V2`;
10. ask from A → still `Local A V1`;
11. ask from B → `Global V2`;
12. reconstruct all application services from the same persisted fake state;
13. ask A/B again → identical results.

This test is non-negotiable.

## 22.5 Mandatory stale-vector regression test

With `FakeVectorStore`:

1. index QA item current version V1 under stable vector id `qa:<item>`;
2. approve V2;
3. reindex item;
4. assert only one vector exists for item;
5. assert metadata `version_id=V2` and text V2;
6. assert V1 cannot be retrieved.

## 22.6 Mandatory legacy-rebuild test

Seed fake vector store with:

```text
qav-old-1
qav-old-2
message-old-id
```

Run v2 migration/rebuild and assert legacy ids are removed and only stable v2 ids remain.

## 22.7 Mandatory background evidence tests

Cover:

- standalone question stored but not indexed;
- chitchat stored and not indexed;
- knowledge update indexed immediately;
- question parent + answer-like reply indexed as combined Q→A;
- budget-deferred message stored but no AI/index call;
- another space cannot retrieve local message evidence;
- configured admin-authored Telegram evidence receives authority 95;
- ordinary Telegram evidence receives authority 40;
- WhatsApp evidence receives authority 50;
- source instance identity is preserved in indexed provenance.

## 22.8 Mandatory reviewer tests

Cover server-side authorization:

```text
local reviewer + approve_local own space -> allow
local reviewer + approve_global -> deny
local reviewer other space -> deny
global reviewer + approve_local -> allow
global reviewer + approve_global -> allow
admin + both -> allow
stranger + any -> deny
resolved feedback + any second decision -> conflict/no extra version
```

## 22.9 Mandatory Telegram adapter tests

With fake Telegram client:

- duplicate webhook delivers answer once;
- if answer persisted but delivery failed, retry delivers it;
- `ok=false` Telegram response is treated as failure;
- empty text does not crash reviewer command parser;
- forged global-approval callback from local reviewer is denied;
- proposal ForceReply interaction survives reconstructing adapter/service objects;
- consumed interaction cannot be reused;
- unauthorized stranger DM is ignored except valid correction interaction reply.

## 22.10 Tier 4 — SQLite integration

Run real migrations against a temporary SQLite file.

Test:

- shared migrations apply;
- local migration applies;
- foreign keys enabled;
- correction transaction rolls back fully on injected failure;
- NumPy vectors persist across process/repository reconstruction;
- scope filters return correct records;
- canonical-identity migration merges duplicates correctly.

This tier is safe in CI.

## 22.11 Tier 5 — local AI E2E

Marker:

```text
local_ai
```

Command:

```bash
make test-e2e-local
```

Requires running local Ollama and SQLite stack.

Tests should be few and robust:

1. embedding batch returns correct count/dimension;
2. direct Q&A answer bypasses generator;
3. one trivial synthesis case returns schema-valid grounded answer citing only allowed source;
4. one insufficient-evidence case abstains;
5. one background message classification/indexing path.

Do not assert exact prose from `gemma3:270m`.

Assert facts such as:

- mode;
- schema;
- citation subset;
- required simple fact present where reliable;
- no unsupported source ids.

Temperature is 0.

## 22.12 Tier 6 — Cloudflare smoke

Marker/command:

```text
cloudflare_live
make smoke-cloudflare
```

Never run automatically.

Must require explicit environment flag:

```text
ALLOW_CLOUDFLARE_LIVE_TESTS=1
```

Without it, exit before any AI call.

Cloudflare smoke target must stay tiny:

- health;
- D1 read/write fixture;
- Vectorize upsert/query with required metadata filters;
- one EmbeddingGemma embedding;
- at most one GLM synthesis request;
- scheduled handler/manual daily-report path;
- cleanup test fixture.

No LLM judge.
No full production reindex.
No loop over a large eval dataset.

---

# 23. Human Telegram E2E

This is a manual acceptance case documented in `docs/e2e-telegram.md`.

Use a **test Telegram bot and test groups**, never the production group for first-run validation.

## 23.1 Local E2E architecture

The application/AI/database remain local.

Telegram requires a publicly reachable HTTPS webhook. Use an optional tunnel only for ingress. The tunnel is not part of the knowledge/AI runtime.

Recommended documented option: `cloudflared` quick tunnel in a third disposable container on the same Docker network, pointing to `http://knowledge-bot-local:8000`.

Do not require Cloudflare account AI/D1/Vectorize for this path.

If quick-tunnel behavior changes, the guide may also show a generic “use any HTTPS tunnel” alternative, but the canonical tested instructions should use one provider.

## 23.2 Manual local Telegram acceptance flow

Execute in this exact order:

1. `make dev-bootstrap`;
2. `make dev-up`;
3. create/register two logical spaces A and B;
4. bind Telegram test group A to space A;
5. bind Telegram test group B to space B;
6. seed one global Q&A with a distinctive answer `Global V1`;
7. open tunnel and register Telegram webhook against local `/channels/telegram/webhook`;
8. ask equivalent question in group A → expect global answer;
9. ask in group B → expect same global answer;
10. enable/confirm background listener;
11. in group A post an unaddressed question, then reply with a clear factual answer;
12. inspect local logs/DB and confirm the Q→A pair is indexed immediately;
13. ask bot an equivalent addressed question in group A and confirm the local evidence is retrievable;
14. flag original global Q&A answer in A;
15. submit proposal in DM;
16. local reviewer receives review;
17. verify local reviewer does **not** receive “approve global” button;
18. local reviewer approves group correction;
19. ask A → local correction;
20. ask B → still global V1;
21. deliberately send/forge an approve-global callback as local reviewer using a test helper → expect server denial and no global version;
22. global reviewer/admin approves a new global correction `Global V2`;
23. ask A → local override still wins;
24. ask B → global V2;
25. restart app container;
26. repeat A/B questions → results unchanged;
27. call `/internal/jobs/daily-report`;
28. admin receives one report containing addressed/background/correction activity;
29. inspect logs for scope/decision metadata;
30. verify no Cloudflare AI/D1/Vectorize usage was required.

## 23.3 Production Cloudflare Telegram acceptance flow

After local acceptance and only with explicit human authorization:

1. deploy v2 Worker to test/staging name if available;
2. use dedicated D1/Vectorize test resources where practical;
3. apply migrations;
4. confirm metadata indexes `kind`, `status`, `scope_key`;
5. bind the same test Telegram groups/spaces;
6. seed tiny fixture;
7. register webhook to Worker;
8. run only steps 8, 18–24, and daily report trigger from the local flow;
9. run no full live eval and no full reindex unless separately authorized;
10. inspect estimated AI usage after test.

---

# 24. Cloudflare production entry and setup

## 24.1 `wrangler.jsonc`

Update configuration to include:

- D1 binding;
- Vectorize binding;
- AI binding;
- existing non-secret vars;
- one Cron Trigger `0 19 * * *`;
- compatibility flags required by current Python Workers.

Do not commit secrets.

## 24.2 Required Vectorize setup

The setup guide must contain explicit commands to create metadata indexes for:

```text
kind
status
scope_key
```

Before issuing commands, the implementing agent must verify exact current Wrangler syntax against installed Wrangler/help output or primary Cloudflare documentation. Do not invent flags.

## 24.3 Required secrets

Document at minimum:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
ADMIN_TELEGRAM_USER_ID
INTERNAL_ADMIN_KEY
```

If allowed chat ids remain configuration, document them too.

Do not store tokens in `wrangler.jsonc`.

## 24.4 Deploy sequence

`docs/setup-cloudflare.md` must prescribe this order:

1. prerequisites/account access;
2. install/sync toolchain;
3. create fresh v2 D1 and Vectorize resources for the canonical cutover;
4. create the `kind`, `status`, and `scope_key` metadata indexes;
5. configure bindings/vars;
6. set secrets;
7. apply all v2 migrations to the fresh D1 database;
8. deploy Worker;
9. run `/healthz`;
10. run tiny D1/Vectorize smoke without large AI workload;
11. seed the committed Q&A and WhatsApp material from `data/seed/`;
12. register spaces/channel bindings;
13. register Telegram webhook;
14. manually test one direct Q&A question;
15. only if explicitly authorized, run the tiny Cloudflare AI smoke;
16. verify Cron trigger registration;
17. stop.

The legacy maintenance and `migrate-v2` commands remain tested for operators who choose to preserve an older non-production D1/Vectorize pair, but they are not part of this repository's canonical disposable production cutover.

Do not tell operators to run `make eval-live-reindex` as routine setup.

---

# 25. Documentation deliverables

Documentation is part of the implementation, not a later task.

Create/update exactly these primary docs:

```text
README.md
docs/architecture.md
docs/setup-local.md
docs/setup-cloudflare.md
docs/e2e-telegram.md
docs/usage.md
docs/development.md
docs/knowledge-base.md
docs/operations.md
AGENTS.md
instructions/qa-telegram-bot-refactor-v2-spec.md
instructions/[deprecated]_pla_prototip_bot_telegram_bhc_v3.md
data/seed/bot_self_qa.json
```

`docs/usage.md` remains the user-facing Telegram guide. Delete the old combined `docs/setup.md` after its still-valid content has moved to `docs/setup-local.md` or `docs/setup-cloudflare.md`; do not leave a duplicate setup page.

## 25.1 `README.md`

Short entry point only:

- what the bot is;
- architecture in one diagram;
- default local setup commands;
- links to detailed docs;
- explicit “local first; Cloudflare live tests are opt-in”.

Do not duplicate every operations detail.

## 25.2 `docs/architecture.md`

Must explain:

- one source of truth vs derived index;
- global/local scopes;
- space abstraction;
- principals/reviewers;
- channel adapter boundary;
- application REST boundary;
- local vs Cloudflare implementations;
- event families;
- Q&A identity/provenance;
- why automatic generated Q&A was removed.

## 25.3 `docs/setup-local.md`

Human-oriented clean-machine guide.

Must start from only:

```text
Git
Docker
make
```

Host Python/Ollama/Node should not be required for canonical local development.

Show:

```bash
make dev-bootstrap
make dev-up
curl http://localhost:8000/readyz
make dev-seed
make test-e2e-local
```

Explain where persistent SQLite/Ollama state lives and how `dev-reset` works.

## 25.4 `docs/setup-cloudflare.md`

Human-oriented production/staging guide following Section 24.

Clearly mark commands that can consume Workers AI quota.

## 25.5 `docs/e2e-telegram.md`

Contain the exact manual local and Cloudflare Telegram E2E cases from Section 23.

## 25.6 `docs/development.md`

Define the test ladder and exactly when to run each target.

Default iteration loop:

```text
make lint
make test
make test-integration
```

For AI changes:

```text
make test-e2e-local
```

Cloudflare only when explicitly authorized.

## 25.7 `docs/knowledge-base.md`

Correct stale statements:

- D1/SQLite source of truth;
- stable Q&A vector projection;
- semantic canonical key, separate web anchor;
- global + space local layers;
- relevant background messages indexed immediately;
- questions/chitchat not evidence;
- human-approved corrections not silently overwritten by seed renewal;
- no `auto_generated` Q&A creation.

## 25.8 `docs/operations.md`

Remove stale claims:

- Python Workers lack cron;
- reset behavior guesses not backed by current primary docs;
- routine full live eval advice;
- full reindex as normal maintenance.

Add:

- scheduled report operations;
- projection repair;
- v2 vector migration;
- quota-safe commands;
- logs;
- common local-vs-Cloudflare diagnosis steps.

## 25.9 `AGENTS.md`

This file is critical.

Add near the top:

```text
DEFAULT DEVELOPMENT MODE: LOCAL ONLY.

An agent MUST NOT invoke Cloudflare Workers AI, remote Vectorize, remote D1
maintenance, live evals, or a production/staging reindex unless the human has
explicitly authorized that operation in the current conversation/task.

Use `make test-e2e-local` for AI-path iteration.
```

Also state:

- required models;
- approved dependencies;
- forbidden dependencies;
- scope/reviewer invariants;
- no opportunistic cron logic;
- update docs in same branch;
- no commits directly to main.

## 25.10 Deprecated implementation plan

`instructions/[deprecated]_pla_prototip_bot_telegram_bhc_v3.md` is historical context only. It must never be presented as current behavior or as a second implementation contract.

Keep its filename and a prominent deprecation notice pointing to `instructions/qa-telegram-bot-refactor-v2-spec.md`. When v2 behavior changes, update the notice's implementation-status summary in the same branch; do not rewrite the historical body to imply that deprecated behavior is current.

## 25.11 Bot self-Q&A

Regenerate/edit `data/seed/bot_self_qa.json` after docs are finalized.

It must correctly state:

- local reviewer/global reviewer/admin flow;
- local/global knowledge behavior;
- background listening behavior;
- no claim that only admin approves every correction;
- no stale report/cron behavior;
- no implementation internals/secrets.

---

# 26. Target source tree

Do not mechanically create empty packages. This is the expected final ownership map.

```text
src/
├── entry.py                         # Cloudflare Worker fetch + scheduled
├── local_entry.py                   # local FastAPI/uvicorn entry
└── knowledge_bot/
    ├── domain/
    │   ├── entities.py
    │   ├── enums.py
    │   ├── errors.py
    │   ├── identity.py              # canonical key + principal helpers
    │   ├── policies.py
    │   └── scope.py
    │
    ├── contracts/
    │   ├── api.py                   # generic REST DTOs
    │   ├── messages.py              # normalized channel-independent inbound DTO
    │   ├── seed.py
    │   └── telegram.py              # Telegram external payload DTOs only
    │
    ├── ports/
    │   ├── budget.py
    │   ├── clock.py
    │   ├── embedder.py
    │   ├── generator.py
    │   ├── http.py
    │   ├── repositories.py
    │   ├── transactions.py          # narrow correction/seed commit ports only
    │   └── vector_store.py
    │
    ├── application/
    │   ├── answer_question.py
    │   ├── background.py
    │   ├── budget.py
    │   ├── corrections.py
    │   ├── daily_report.py
    │   ├── indexing.py
    │   ├── ingest.py
    │   ├── intake.py
    │   ├── maintenance.py
    │   ├── retrieval.py
    │   ├── reviewers.py
    │   ├── review.py
    │   ├── seed.py
    │   └── revert.py
    │
    ├── adapters/
    │   ├── http/
    │   │   ├── app.py
    │   │   ├── internal_routes.py
    │   │   └── api_routes.py
    │   ├── telegram/
    │   │   ├── inbound.py
    │   │   ├── rendering.py
    │   │   ├── routes.py
    │   │   └── client.py
    │   └── importers/
    │       ├── web_snapshot.py
    │       └── whatsapp_export.py
    │
    └── infrastructure/
        ├── clock.py
        ├── logging.py
        ├── metering.py
        ├── settings.py
        ├── local/
        │   ├── composition.py
        │   ├── http.py
        │   ├── ollama.py
        │   ├── vectors.py
        │   └── sqlite/
        │       ├── common.py
        │       ├── messaging.py
        │       ├── knowledge.py
        │       ├── corrections.py
        │       └── operations.py
        └── cloudflare/
            ├── composition.py
            ├── http.py
            ├── vectorize.py
            ├── workers_ai.py
            └── d1/
                ├── common.py
                ├── messaging.py
                ├── knowledge.py
                ├── corrections.py
                └── operations.py
```

## 26.1 Files to retire

After equivalent code/tests exist, delete:

```text
src/knowledge_bot/adapters/inbound/fastapi_routes.py
```

Move/replace old Telegram module paths rather than keeping wrappers indefinitely.

The implementation may temporarily keep old modules during intermediate commits, but final branch must not contain compatibility forwarding modules unless an external import outside the repository requires them and the human approves.

## 26.2 D1 file split rule

Split the current very large `d1.py` by bounded concern as shown above.

Do not create one repository class per file.
Do not invent a generic `BaseRepository`.

---

# 27. Python coding rules

These rules are mandatory for the refactor.

## 27.1 Prefer plain functions for pure policy

Examples:

```text
canonical_key_for
scope_for_space
review authorization
answer evidence selection
report rendering
```

Do not create a class when there is no state/dependency to hold.

## 27.2 Dataclasses for domain/application values

Prefer:

```python
@dataclass(frozen=True, slots=True)
```

for immutable result/value objects.

Use mutable dataclass only when the object intentionally owns a cache, e.g. classifier prototype embeddings.

## 27.3 Pydantic only at boundaries

Use Pydantic for:

- REST input/output;
- Telegram payloads;
- settings;
- external AI JSON.

Do not convert every internal dataclass to Pydantic.

## 27.4 Enums for closed domains

Use `StrEnum` for:

```text
runtime mode
intent labels
answer mode
feedback status
review action
classification status
index status
AI work class
```

Do not pass magic strings through core logic.

## 27.5 Type rules

- full annotations;
- avoid `Any`;
- no `cast()` to hide obviously invalid data unless boundary interop requires it;
- validate external dictionaries once at boundary;
- `zip(..., strict=True)` when list lengths must match;
- no mutable default arguments.

## 27.6 Control flow

Prefer early returns and short private helpers.

Do not silence Ruff complexity globally. Existing `C901` ignore may remain initially, but refactored route/application functions should naturally become smaller.

## 27.7 Exceptions

Define meaningful errors in `domain/errors.py` or application-specific errors where appropriate:

```text
NotFoundError
AuthorizationError
InvalidTransitionError
ModelUnavailableError
ProjectionError
```

Do not use exceptions for normal abstention.

## 27.8 Time

All application time comes from `Clock`.

Only infrastructure clock implementation calls `datetime.now(UTC)`.

## 27.9 IDs

Centralize id construction helpers where ids carry semantics.

Do not concatenate ids independently in five routes.

Use UUIDs for new durable objects where idempotency does not require a deterministic id.

Idempotency keys derived from external events should remain deterministic.

## 27.10 Async

I/O-facing application services remain async.

Local SQLite uses `aiosqlite`.
Ollama uses async `httpx`.
Do not call synchronous HTTP or `time.sleep()` from request paths.

The sync Typer CLI may use sync wrappers around async operations via `asyncio.run()` at command boundaries.

---

# 28. Implementation phases and required commits

Use small, reviewable commits. Commit names below are recommended and should be followed unless repository convention demands a minor wording change. Deliver the phases in the five branch groups defined in Section 0.1; do not open one branch for all twelve phases.

## Phase 0 — branch and baseline

The first implementation branch is:

```bash
git switch -c refactor/v2-correctness
```

Run before modifications:

```bash
make lint
make test
make test-integration
```

If baseline fails, record exact failures before changing behavior.

Commit only if documentation/tests need a baseline note; otherwise no commit.

## Phase 1 — characterize and fix immediate correctness defects

Commit:

```text
:bug: Lock core knowledge invariants
```

Changes:

- direct answers carry/persist `qa_version_id`;
- add tests for direct provenance;
- add stale-vector regression around stable conceptual item (temporary implementation acceptable here only if next phase replaces ids);
- empty reviewer command bug fixed;
- injected clock used for reviewer events;
- resolved feedback cannot be decided twice;
- Telegram `ok=false` treated as delivery failure;
- shared in-memory backend fixture introduced for new integration tests.

Do not move major files yet.

Gates:

```bash
make lint
make test
make test-integration
```

## Phase 2 — spaces and principals

Commit:

```text
:recycle: Decouple spaces and principals from Telegram
```

Changes:

- migrations 0011;
- domain scope/principal helpers;
- channel binding repository;
- backfill-spaces maintenance service/CLI;
- separate `SourceType` from `Source.id`: replace type-derived ids with the exact source-instance id rules from Section 5.6;
- migrate/backfill legacy source references deterministically before enforcing new ids;
- reviewer representation uses principal ids;
- answer/retrieval application paths receive `space_id`;
- Telegram adapter resolves group binding.

Tests:

- global/local visibility by space;
- two channel bindings could map to same space fixture;
- two WhatsApp imports for different scopes cannot collapse into one source instance;
- repeated snapshots of the same web URL reuse the same durable web source instance;
- no application code uses Telegram chat id as scope;
- no ingestion code derives `Source.id` from `SourceType.value`.

## Phase 3 — canonical identity and stable projection

Commit:

```text
:bug: Repair Q&A identity and search projection
```

Changes:

- migration 0012 + 0016;
- semantic canonical key;
- source anchor on version;
- normalize-knowledge-identity maintenance command;
- stable vector ids `qa:<item>` / `msg:<message>`;
- projection manifest;
- v2 legacy vector cleanup command;
- Vectorize metadata changes to `scope_key`;
- seed renewal authority behavior.

Run comprehensive SQLite migration tests.

Do not run Cloudflare migration yet.

## Phase 4 — background pipeline

Commit:

```text
:recycle: Index only evidence-bearing background messages
```

Changes:

- migration 0013;
- instance classifier cache;
- classification returns embedding;
- small deterministic prefilter;
- all background messages persisted;
- eligibility policy;
- immediate index;
- budget-deferred state;
- bounded backlog processor;
- message search source lists only evidence-eligible/indexed messages.

Tests from Section 22.7 mandatory, plus explicit authority tests:

- configured admin-authored Telegram evidence indexes at authority 95;
- ordinary Telegram evidence indexes at authority 40;
- WhatsApp evidence indexes at authority 50;
- the same message text does not receive admin authority merely because it came from the Telegram source type.

## Phase 5 — simplify answering/AI

Commit:

```text
:recycle: Simplify grounded answer generation
```

Changes:

- message_top_k default 4;
- evidence cap 5;
- remove runtime judge method;
- remove automatic Q&A generation path/concept;
- strict generation output validation;
- provenance-rich `AnswerOutcome`;
- no model call for direct Q&A.

Offline tests only at this point.

## Phase 6 — corrections/review authorization

Commit:

```text
:bug: Enforce scoped reviewer authority
```

Changes:

- `PENDING_REVIEW` semantics;
- `ReviewAction` enum;
- pure authorization policy;
- narrow correction transaction port;
- local reviewer cannot global approve;
- duplicate resolution protected;
- local/global/admin tests.

## Phase 7 — generic REST + thin Telegram

Commit:

```text
:recycle: Separate channel adapters from REST application API
```

Changes:

- Pydantic generic API contracts;
- split HTTP modules;
- move Telegram parsing/render/client/routes into adapter package;
- route code stops touching repositories/service internals;
- migration 0014;
- durable Telegram interaction correlation;
- delivery receipts/idempotent delivery.

Delete old monolithic route module at end of phase.

## Phase 8 — fully local runtime

Commit:

```text
:sparkles: Add fully local Docker development runtime
```

Changes:

- approved local dependencies;
- multi-stage Dockerfile;
- local SQLite repositories;
- local vector table/migration;
- NumPy vector store;
- `HttpxClient`;
- `OllamaEmbedder`;
- `OllamaGenerator`;
- local composition;
- `src/local_entry.py`;
- Makefile dev commands;
- `.env.local.example`;
- local readiness;
- local AI tests.

Run:

```bash
make dev-bootstrap
make dev-up
make test-e2e-local
```

## Phase 9 — daily report and Cloudflare scheduled handler

Commit:

```text
:recycle: Replace opportunistic recaps with daily scheduled report
```

Changes:

- migration 0015;
- deterministic DailyReportService;
- remove opportunistic calls from message routes;
- explicit WorkerEntrypoint fetch + scheduled;
- cron config;
- manual daily report endpoint;
- old recap/report service retirement;
- remove obsolete settings `RECAP_ENABLED`, `RECAP_INTERVAL_HOURS`, `RECAP_LANGUAGE`, `ADMIN_REPORT_MODE`, and `ADMIN_REPORT_INTERVAL_MIN` from active v2 configuration.

Do not run Cloudflare live smoke automatically.

## Phase 10 — infrastructure decomposition and settings cleanup

Commit:

```text
:recycle: Simplify composition and persistence adapters
```

Changes:

- split D1 module by concern;
- final local SQLite module split;
- separate local/cloudflare composition;
- settings validation through Pydantic only;
- remove manual silent coercion;
- no raw repos in route context;
- clean dead code/imports.

This is intentionally late so file moves do not obscure logic fixes.

## Phase 11 — documentation

Commit:

```text
:memo: Align setup and behavior documentation with v2
```

Update every deliverable from Section 25.

Run link/command review manually.

## Phase 12 — final QA

Commit if fixes are required:

```text
:white_check_mark: Harden v2 acceptance coverage
```

Run:

```bash
make format
make lint
make test
make test-integration
make dev-up
make test-e2e-local
```

Then perform human local Telegram E2E.

Only after explicit authorization perform Cloudflare smoke/migration/deploy work.

---

# 29. Deletions and simplifications required by the final state

Final v2 code should no longer contain active behavior for:

- runtime LLM judge;
- automatic Q&A creation from synthesis;
- opportunistic daily recap check on every inbound message;
- opportunistic reviewer batch report check on every inbound message;
- local reviewer global approval;
- Telegram ForceReply methods on a channel-generic core transport port;
- Telegram chat ids used directly as Q&A scope;
- web source anchors used as semantic canonical keys;
- version ids as Q&A vector ids;
- full reindex that indexes every text message;
- module-global classifier vector cache;
- silent invalid settings fallback;
- route reach-through into nested service repositories;
- direct application calls to `datetime.now`;
- normal development dependence on Cloudflare AI/Vectorize.

Delete obsolete tests that assert these old behaviors. Replace them with v2 assertions; do not keep contradictory tests skipped.

---

# 30. Agent “do not improvise” list

The implementing agent MUST NOT:

1. change the selected local or production models;
2. add ONNX because it thinks it is faster;
3. add FAISS/Chroma because it recognizes “vector search”;
4. add SQLAlchemy because there are two SQL adapters;
5. add a DI framework;
6. add generic repository base classes;
7. add a service locator;
8. add an event bus/queue;
9. split into microservices;
10. add Docker Compose;
11. add PydanticAI/LangChain;
12. create an agent loop;
13. run Cloudflare live evals during iteration;
14. run a full remote reindex without explicit human authorization;
15. auto-retry a failed remote AI suite;
16. store knowledge only in Vectorize/local vectors;
17. create Q&A automatically from generated synthesis;
18. let a local reviewer approve global knowledge;
19. use Telegram chat ids as final scope keys;
20. keep stale compatibility wrappers “just in case”;
21. weaken tests to make the new architecture pass;
22. log raw production message content;
23. catch broad exceptions in core and continue silently;
24. invent Cloudflare/Ollama flags without checking primary docs/help;
25. edit the production data manually to make tests pass.

---

# 31. Acceptance criteria / definition of done

The refactor is complete only when all of these are true.

## 31.1 Correctness

- [ ] Direct Q&A answers persist exact cited version id.
- [ ] One semantic Q&A identity is used across seed/correction paths.
- [ ] Source anchor is provenance, not identity.
- [ ] Only current Q&A version is searchable under one stable item vector id.
- [ ] Legacy stale vectors are removed by explicit migration.
- [ ] Local Q&A suppresses global Q&A of same canonical key only in its space.
- [ ] Other spaces never retrieve local knowledge.
- [ ] Standalone questions are never factual evidence.
- [ ] Distinct WhatsApp/web source instances cannot collapse because of shared source-type ids.
- [ ] Admin-authored Telegram evidence receives the documented higher authority through one tested policy.
- [ ] Relevant background evidence becomes searchable immediately.
- [ ] Local reviewer cannot approve global knowledge, including forged request.
- [ ] Global reviewer/admin can approve global.
- [ ] Resolved feedback cannot be resolved twice.
- [ ] SQL correction commit is atomic.
- [ ] Index failure cannot corrupt semantic SQL state.

## 31.2 Architecture

- [ ] Core uses spaces/principals, not Telegram ids.
- [ ] Telegram is a thin adapter.
- [ ] Generic REST contracts exist for question/feedback/review workflows.
- [ ] All workflow state is durable SQL state.
- [ ] No route reaches through a service into a repository.
- [ ] Local and Cloudflare composition roots share application services.
- [ ] One Worker remains the production deployable.

## 31.3 Local development

- [ ] Fresh machine with Git + Docker + make can follow `docs/setup-local.md`.
- [ ] `make dev-bootstrap` needs no Cloudflare credentials.
- [ ] Local app uses SQLite.
- [ ] Local search uses NumPy.
- [ ] Local AI uses Ollama `embeddinggemma` + `gemma3:270m`.
- [ ] `make test-e2e-local` consumes no Cloudflare resources.
- [ ] Local state survives app container restart.
- [ ] Local reset is explicit/destructive only with confirmation.

## 31.4 Production

- [ ] D1 remains semantic source of truth.
- [ ] Vectorize remains derived.
- [ ] Vectorize metadata indexes exist for kind/status/scope_key.
- [ ] Scheduled daily report uses Python Worker scheduled handler.
- [ ] No paid fallback exists.
- [ ] Background AI has a lower-priority budget ceiling.
- [ ] Production log-content setting cannot be enabled accidentally.

## 31.5 Tests

- [ ] Ruff format/check pass.
- [ ] `ty` passes.
- [ ] unit tests pass.
- [ ] architecture tests pass.
- [ ] shared-backend integration tests pass.
- [ ] SQLite integration tests pass.
- [ ] local AI E2E passes.
- [ ] manual local Telegram E2E passes.
- [ ] Cloudflare smoke, if authorized, passes without full reindex/live eval.

## 31.6 Documentation

- [ ] README points to correct local-first workflow.
- [ ] local setup guide tested from clean state.
- [ ] Cloudflare setup guide tested/validated.
- [ ] Telegram E2E guide exists.
- [ ] AGENTS forbids unapproved Cloudflare live work.
- [ ] old implementation plan contains no contradictory no-cron/admin-only/auto-Q&A statements.
- [ ] bot self-Q&A reflects real v2 behavior.

---

# 32. Final human review checklist before merging

A human reviewer should answer yes to all of the following:

1. Can I understand global vs local behavior without knowing Telegram internals?
2. Can I run the complete app locally without a Cloudflare account?
3. Can I distinguish a functional bug from a Cloudflare adapter problem?
4. Does a correction provably update the Q&A item that produced the answer?
5. Can an old Q&A vector survive after a correction? It must not.
6. Can a plain question from background conversation become evidence? It must not.
7. Can a local reviewer change global truth? It must not.
8. Does restarting the app lose pending corrections/review interactions? It must not.
9. Does routine testing touch remote AI? It must not.
10. Is every Cloudflare-expensive command visibly opt-in?
11. Are docs and bot self-description consistent with code?
12. Are there materially fewer concepts than before despite stronger correctness?

If any answer is unclear, do not merge.

---

# 33. Verified external platform facts used by this design

These facts were checked against primary/current documentation while preparing this specification. Re-check exact CLI syntax during implementation because CLIs evolve.

## Ollama

- Local API base URL is `http://localhost:11434/api`.
- Embeddings use `POST /api/embed` and support batched `input` arrays.
- Ollama embedding responses are normalized vectors suitable for cosine similarity.
- Structured generation accepts a JSON schema through the `format` field; Pydantic's `model_json_schema()` is an intended usage.
- Temperature 0 is recommended for deterministic structured outputs.
- `embeddinggemma` is a recommended embedding model and is multilingual.
- `gemma3:270m` is an available 268M-parameter CPU-friendly model in Ollama, with a small quantized artifact.

Primary references:

- https://docs.ollama.com/api
- https://docs.ollama.com/capabilities/embeddings
- https://docs.ollama.com/capabilities/structured-outputs
- https://ollama.com/library/embeddinggemma
- https://ollama.com/library/gemma3:270m

## Cloudflare

- Workers AI has no true local simulation; local Workers development connects AI remotely when used.
- Vectorize has no local simulation; remote binding is required for actual Vectorize behavior.
- D1 has local simulation, but this project intentionally uses direct SQLite locally to isolate application logic from Cloudflare runtime behavior.
- Python Workers support `scheduled()` handlers through `WorkerEntrypoint`.
- Cron Triggers invoke scheduled handlers and are available on the Free plan.
- D1 `batch()` is transactional: a failed statement rolls back the batch.
- FastAPI can run in Python Workers through the ASGI integration; an explicit `WorkerEntrypoint.fetch()` can call ASGI fetch when a custom entrypoint is needed.

Primary references:

- https://developers.cloudflare.com/workers/local-development/bindings-per-env/
- https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/
- https://developers.cloudflare.com/workers/configuration/cron-triggers/
- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/d1/worker-api/d1-database/
- https://developers.cloudflare.com/workers/languages/python/packages/fastapi/

---

# 34. Final instruction to the implementing agent

Implement the product described here, not the accidental behavior of the current code.

The intended result is deliberately boring:

```text
clear SQL truth
+ two scopes
+ deterministic permissions
+ small retrieval policy
+ one optional synthesis call
+ relevant background evidence only
+ thin channel adapters
+ fully local development
+ small Cloudflare production adapters
```

When choosing between a clever abstraction and an explicit 20-line implementation, choose the explicit implementation.

When choosing between another model call and a deterministic rule, choose the deterministic rule.

When choosing between preserving a broken internal API and simplifying it, simplify it and update all call sites/tests/docs in the same branch.

When choosing between testing against Cloudflare and reproducing the behavior locally, reproduce it locally first.

Do not declare the refactor complete until the manual local Telegram E2E has passed and the documentation has been reread against the final code.
