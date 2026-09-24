# QA Telegram Bot — One-Pass Final Fix Plan

**Repository:** `uladribia/qa-telegram-bot`
**Base revision reviewed:** `f5224fe0a9cc184e1f8011140ae24b620a0a16e2`
**Purpose:** finish the remaining hardening defects in one implementation pass without redesigning the system
**Audience:** coding agent with limited judgment; this document is intentionally prescriptive
**Status:** binding implementation plan for this pass

---

# 0. Mandatory operating rules

The coding agent MUST follow this document literally.

This is **not** a redesign task.

The current architecture is broadly correct and MUST be preserved:

```text
SQL semantic truth
+ global / space-local scopes
+ stable derived vector ids
+ explicit projection repair
+ direct Q&A or one grounded generation call
+ background evidence only
+ Telegram as an adapter
+ local SQLite + NumPy + Ollama
+ production D1 + Vectorize + Workers AI
```

The agent MUST NOT:

1. introduce a new framework;
2. add SQLAlchemy, SQLModel, an ORM, Redis, a queue, Kafka, Celery, LangChain, PydanticAI, FAISS, Chroma, ONNX, or Docker Compose;
3. replace Ollama in local development;
4. replace the current Cloudflare AI models;
5. redesign the REST API;
6. add a service bus/event bus;
7. split the application into microservices;
8. add backward-compatibility shims;
9. change product behavior beyond what is explicitly required here;
10. retune semantic thresholds;
11. rewrite large modules simply for style;
12. run a remote reindex, live eval, or production mutation unless explicitly authorized by the user;
13. weaken, skip, or delete a regression test to make the implementation pass;
14. infer missing behavior from old docs when this document specifies the behavior;
15. mark the task complete if any final acceptance item in Section 18 is not satisfied.

When this plan says **exactly**, do not substitute an alternative architecture.

---

# 1. Work on one branch, one pass

Create exactly one branch from current `main`:

```bash
git switch main
git pull --ff-only
git switch -c fix/final-hardening-pass
```

Do not create multiple phase branches.

Do not merge intermediate work into `main`.

The implementation may use several commits, but all work belongs to this one branch.

Recommended commit sequence:

```text
1. :bug: Fix bounded rebuild orchestration
2. :bug: Correct retrieval ordering and pairing lifecycle
3. :bug: Harden correction projection and callback recovery
4. :white_check_mark: Add Telegram and retry regressions
5. :chore: Add offline CI and live-operation guards
6. :recycle: Finish adapter SQL and logging cleanup
7. :memo: Reconcile final implementation documentation
```

After every commit run:

```bash
make lint
make test
make test-integration
```

Do not proceed to the next commit with a red gate.

---

# 2. Scope of this pass

Fix **only** the remaining defects listed below.

## P0

### F1 — remote/full rebuild can embed the corpus twice

Current behavior:

```text
CLI calls POST /internal/reindex with rebuild=true
→ server ReindexService.rebuild()
→ deletes projection
→ immediately performs unbounded self.reindex()
→ CLI then enters normal batched reindex loop
→ whole corpus may be embedded twice
```

This is dangerous and can consume the Cloudflare Workers AI daily quota.

Required final behavior:

```text
new kb reindex run
→ cleanup only, zero AI
→ batch 1
→ print cursors
→ batch 2
→ ...
```

Resume behavior:

```text
kb reindex --qa-after ... and/or --msg-after ...
→ skip cleanup
→ continue batched indexing
```

There must be **no unbounded server-side full reindex operation**.

---

## P1 correctness

### F2 — local/global retrieval final ordering is wrong

Current `_merge_group_first()`:

```text
sort local
remove same-canonical globals
concatenate all local first
then globals
truncate
```

This lets weak unrelated local candidates evict much stronger global candidates.

Required behavior:

1. query global and local independently;
2. local candidate suppresses the global candidate only when both share the same `canonical_key`;
3. combine remaining candidates;
4. sort the final combined list by:
   - similarity descending;
   - authority descending;
5. truncate to `qa_top_k`.

Local scope is a **canonical override**, not a blanket priority over unrelated global knowledge.

---

### F3 — pairing lifecycle confuses “zero accepted pairs” with “processing failed”

Current pattern:

```python
if old_window_is_due:
    if not await self._process_window(window):
        return 0
```

`_process_window()` returns number of accepted pairs.

Therefore a successfully processed window with zero candidates returns `0`, which is incorrectly interpreted as failure.

Required behavior must distinguish:

```text
processed successfully, accepted 0
```

from:

```text
not processed because budget/model unavailable
```

A successfully processed zero-pair window MUST close and the incoming message MUST start a new pending window.

---

### F4 — failed temporal-pair projection can erase the persisted context question

Current temporal pairing flow persists:

```python
replace(answer, context_question=question.text)
```

but on projection failure later saves:

```python
replace(answer, index_status=FAILED)
```

using the old `answer` object.

That can overwrite the row and remove `context_question`.

Required behavior:

```text
accepted pair
→ persist answer with context_question
→ set pending/index state from that UPDATED object
→ projection succeeds => INDEXED
→ projection fails => FAILED, context_question remains intact
```

Repair and rebuild must therefore reconstruct the same Q→A evidence.

---

### F5 — semantic correction approval can commit successfully but surface as request failure

Current correction flow:

```text
commit Q&A version + item + feedback
→ call projection
→ projection throws
→ API/Telegram path may 500 or fail to acknowledge callback
```

This is wrong because semantic SQL truth has already committed.

Required behavior:

```text
semantic approval commits
→ projection attempted
→ success: projection_status=indexed
→ failure: projection_status=failed
→ semantic approval remains approved
→ durable projection state remains failed/pending and repairable
→ Telegram callback is acknowledged
→ user/reviewer receives success-with-index-warning behavior, not a fake rollback/failure
```

Do NOT roll back semantic approval because Vectorize/embedding failed.

---

### F6 — background backlog request maximum is still 1000

Final contract:

```python
BackgroundBacklogRequest.limit
default = 100
minimum = 1
maximum = 100
```

No 1000-message request.

The application already rechecks budget per item; preserve that behavior.

---

### F7 — one recognized callback path can return without acknowledging Telegram

For recognized `feedback:start`, when `FeedbackService.start(...)` returns `None`, the callback currently returns `"ignored"` without `answer_callback()`.

Every recognized callback MUST be acknowledged exactly once even when:

- target no longer exists;
- feedback is already resolved;
- actor is forbidden;
- start cannot proceed;
- edit prompt cannot be sent;
- approval projection fails.

Unknown/malformed callback data may remain ignored without acknowledgement if it cannot be identified as one of our callbacks.

---

## P1 acceptance / coverage

### F8 — no real synthetic Telegram local E2E

Current local full-flow E2E primarily exercises `/v1/questions` and `/v1/feedback`.

Required test must exercise:

```text
synthetic Telegram Update
→ POST /telegram/webhook
→ Telegram parsing/normalization
→ channel binding
→ application
→ real SQLite
→ real local NumPy vector search
→ real local Ollama embedding
→ fake final Telegram network
```

No real Telegram token/network.

---

### F9 — no full local background temporal-pair E2E

Required local E2E:

```text
group A unaddressed question
→ unaddressed answer
→ next message after quiet period
→ previous window processed
→ temporal pair persisted as message context
→ ordinary msg:<answer> evidence projected
→ addressed semantically equivalent question in A can retrieve/use it
→ same question in B cannot retrieve A evidence
```

Use real local SQLite/NumPy/Ollama.

Do not assert exact generative prose.

---

### F10 — answer delivery retry regression is missing

Add a deterministic integration test proving:

```text
first webhook
→ inbound stored
→ answer stored
→ Telegram delivery fails
→ no delivery receipt

same Telegram update retried
→ no second answer generation
→ stored answer delivered successfully
→ one delivery receipt created

third retry
→ delivery receipt suppresses duplicate Telegram send
```

This test is mandatory even if current implementation already works.

---

## P1/P2 engineering gates

### F11 — no offline GitHub Actions CI

Add:

```text
.github/workflows/ci.yml
```

It must run only offline deterministic gates.

Required jobs/steps:

```bash
uv sync --frozen
make lint
make test
make test-integration
uv run python -m evals.run offline
```

CI MUST NOT:

- receive Cloudflare secrets;
- receive Telegram secrets;
- call Ollama;
- pull Ollama models;
- run Docker;
- call Telegram;
- call Cloudflare;
- run live eval;
- run remote seed;
- run reindex;
- run smoke-cloudflare.

---

### F12 — some Make targets self-authorize remote operations

Current targets set `ALLOW_CLOUDFLARE_LIVE_TESTS=1` themselves.

That defeats the explicit human opt-in.

Final rule:

```text
Caller sets ALLOW_CLOUDFLARE_LIVE_TESTS=1.
Make verifies it.
Make never creates the authorization itself.
CLI verifies it again.
```

Apply this to:

```text
eval-live
eval-live-reindex
seed-self-qa
smoke-cloudflare
any other remote mutation / AI target
```

`BOT_BASE_URL` must be explicit.

No remote target may default to a deployed URL.

---

### F13 — binding hardening plan referenced by AGENTS is missing from repository

`AGENTS.md` currently tells agents to read:

```text
instructions/qa-telegram-bot-final-hardening-plan.md
```

but the file is absent from the repository.

Fix exactly one of these ways:

**Preferred:** add the final hardening plan file to that exact repository path.

Use the existing plan content as the source; do not create a new incompatible plan.

Then add this document as:

```text
instructions/qa-telegram-bot-one-pass-final-fix-plan.md
```

and update `AGENTS.md` so the precedence is:

```text
one-pass-final-fix-plan
supersedes
final-hardening-plan
supersedes
refactor-v2-spec
```

Do not leave references to nonexistent files.

---

### F14 — session handoff is stale

Rewrite `docs/session-handoff.md` to reflect current reality after this branch.

It must not claim:

- an old refactor branch is current;
- work is unmerged when it is merged;
- only manual Telegram acceptance remains if code defects remain;
- Cloudflare acceptance is pending when it was already performed, or vice versa.

Keep it short and factual.

---

## P2 cleanup required by the original hardening contract

### F15 — generic transport port still contains Telegram mechanics

Current `ports/transport.py` still exposes methods such as:

```text
send_force_reply
answer_callback
send_review with inline-button semantics
```

Final design:

- Telegram-specific outbound interaction primitives belong only in `adapters/telegram/client.py`;
- application/core ports MUST NOT expose ForceReply or callback operations.

If `MessageTransport` has no real remaining channel-independent use, delete it.

Do not create a second generic abstraction.

Search all imports and remove it completely if unused after this cleanup.

---

### F16 — Telegram flow ownership remains mostly in generic HTTP app

`TelegramFlow` currently wraps a handler, but the substantial Telegram orchestration still lives in `adapters/http/app.py`.

Do a **narrow extraction**, not a redesign.

Move Telegram-specific functions from:

```text
adapters/http/app.py
```

to:

```text
adapters/telegram/flow.py
```

The final `TelegramFlow` should own:

```text
normalize Update
resolve Telegram-bound space
reviewer Telegram commands
feedback Telegram callbacks
ForceReply correlation
Telegram delivery receipts
Telegram outbound UI behavior
```

It may call application services through `AppContext`.

Do not move generic application use cases into Telegram.

`adapters/telegram/routes.py` should authenticate webhook and call `TelegramFlow.handle()`.

`adapters/http/app.py` should primarily:

- create FastAPI app;
- register generic API routes;
- register Telegram route adapter;
- register internal maintenance routes.

Do not move generic `/v1/*` routes there.

---

### F17 — legacy reviewer report service remains as dead compatibility code

`ReviewerReportService` now does nothing useful.

Delete it and delete any dead rendering/helpers/settings/ports kept solely to support it.

Do not retain “legacy audit service” compatibility code.

Historical reviewer events may remain in SQL because the daily report uses/audits them.

---

### F18 — neutral SQL layer still re-exports Cloudflare D1 classes

Current:

```text
infrastructure/sql/repositories.py
→ imports D1* classes from infrastructure/cloudflare/d1.py
```

This does not satisfy runtime-neutral persistence ownership.

Required final structure:

```text
infrastructure/sql/
    protocol.py
    repositories.py
```

`repositories.py` contains the shared SQL repository implementations themselves, with neutral names, for example:

```text
SqlSourceRepository
SqlConversationRepository
SqlMessageRepository
SqlQAItemRepository
...
```

Cloudflare and SQLite provide only the database execution binding/protocol.

Expected direction:

```text
local SQLiteBinding ─┐
                     ├→ shared Sql*Repository classes
D1 binding ──────────┘
```

Do not create a generic `BaseRepository`.

Do not create one file per repository.

The goal is simply to move shared SQL implementations out of `cloudflare/d1.py`.

Cloudflare-specific code should retain only Cloudflare-specific execution/binding/vector/AI composition.

Update local and production composition roots to instantiate the neutral `Sql*` repositories.

---

### F19 — privacy-safe boundary logging is still absent

Implement the logging contract narrowly.

Keep exactly one sink configuration module.

Required runtime behavior:

```text
local       -> human-readable DEBUG
cloudflare  -> JSON/serialized INFO
```

Add safe structured logs at boundaries only.

At minimum log:

- webhook start/end;
- safe update/request id;
- resolved action (`ignored`, `answer`, `ingest`, correction action);
- space id;
- answer mode;
- evidence counts;
- projection state change/result;
- model operation + duration + timeout/failure class;
- background label/score;
- repair/reindex counts;
- daily report run result.

Never log:

- raw message text;
- generated answer;
- reporter proposal;
- sender name;
- username;
- phone number;
- raw Telegram update;
- prompt;
- tokens;
- secrets.

Do not import Loguru into `domain/` or `application/`.

---

# 3. Phase A — fix rebuild semantics first

This is the first code change because it is the only P0.

## 3.1 Server-side reindex API

Change `ReindexService`.

Final public application methods:

```python
async def reindex(
    qa_after: str | None = None,
    msg_after: str | None = None,
    limit: int = 50,
) -> ReindexReport:
    ...

async def cleanup_projection(self) -> ProjectionCleanupReport:
    ...
```

Do not keep a method named `rebuild()` that both cleans and indexes.

If keeping `rebuild()` temporarily during edit, delete it before final commit.

`cleanup_projection()` performs **zero embedding/model calls**.

It must:

1. delete manifest-known vector ids;
2. delete legacy Q&A version ids;
3. delete legacy raw message vector ids;
4. delete legacy `pair:*` vector ids;
5. clear projection manifest;
6. return counts.

If current `SearchIndexSource.list_legacy_vector_ids()` returns all legacy ids at once, it may remain for this prototype if the dataset is small, but the cleanup operation itself must not invoke embeddings.

Do not call `self.reindex()` from cleanup.

---

## 3.2 HTTP routes

Replace accidental rebuild semantics.

Do NOT use:

```text
empty body == rebuild
```

Final endpoints:

```text
POST /internal/index/cleanup
POST /internal/reindex
POST /internal/index/repair
```

### `/internal/index/cleanup`

- authenticated internal key;
- performs cleanup only;
- returns deletion counts;
- performs no embeddings or generation calls.

### `/internal/reindex`

- authenticated;
- **always bounded**;
- body requires or defaults to a bounded limit;
- no `rebuild` boolean;
- max limit `<=100`;
- performs one batch only.

### `/internal/index/repair`

keep existing semantics.

Delete `ReindexRequest.rebuild`.

Do not keep `/internal/index/rebuild` as an alias.

This prototype explicitly does not need backward compatibility.

---

## 3.3 CLI

`kb reindex` orchestrates the full rebuild.

### New run: neither cursor supplied

Sequence:

```text
POST /internal/index/cleanup
print cleanup result
qa_after = None
msg_after = None

loop:
    POST /internal/reindex limit=<batch>
    print counts and next cursors
    if both cursors None:
        stop
```

### Resume run: either cursor supplied

Sequence:

```text
DO NOT CLEAN
loop from provided cursor(s)
```

After every successful batch print a valid resume command such as:

```bash
uv run kb reindex \
  --base-url "$BOT_BASE_URL" \
  --qa-after '<value>' \
  --msg-after '<value>' \
  --batch 50
```

If one cursor is absent, omit that option or represent it in the exact CLI-supported way.

On interruption, print the last confirmed cursors.

Do not auto-retry AI batches more than the existing bounded transient HTTP retry policy.

Do not start over after a partial failure.

---

## 3.4 Tests

Add/modify tests proving:

1. cleanup does not call embedder;
2. cleanup deletes manifest and legacy ids;
3. one `/internal/reindex` call indexes at most `limit` Q&A/messages;
4. HTTP empty body no longer means destructive cleanup;
5. CLI no-cursor flow calls cleanup exactly once and then batches;
6. CLI resume flow never calls cleanup;
7. a fake corpus of more than one batch is indexed exactly once per record;
8. no duplicate embedding caused by cleanup.

---

# 4. Phase B — retrieval ordering

Edit:

```text
src/knowledge_bot/application/retrieval.py
```

Replace `_merge_group_first()` with behavior equivalent to:

```python
def _rank(match: VectorMatch) -> tuple[float, int]:
    return (
        match.score,
        _as_int(match.metadata.get("authority")),
    )
```

Algorithm:

```text
local_by_key = canonical keys represented locally

candidates =
    all local matches
    +
    global matches whose canonical_key is NOT represented locally

sort candidates descending by:
    score
    authority

return first top_k
```

Important:

- local same-key override survives even if global similarity is higher;
- unrelated local candidates receive no artificial priority;
- a candidate without canonical key suppresses nothing;
- tie-break authority must be deterministic.

Required tests:

### Test 1 — local override

```text
global equipment score=.99 canonical=equipment
local equipment score=.71 canonical=equipment

result contains local equipment
result excludes global equipment
```

### Test 2 — unrelated global survives

```text
5 weak local unrelated scores .20-.30
1 strong global score .99
top_k=5

strong global MUST be in result
```

### Test 3 — authority tie break

```text
two remaining candidates score=.80
authority 40 and 90

authority 90 comes first
```

Do not change retrieval thresholds.

---

# 5. Phase C — temporal pairing lifecycle and persistence

Edit:

```text
src/knowledge_bot/application/listener_pairing.py
```

## 5.1 Introduce one explicit result value

Add exactly:

```python
@dataclass(frozen=True, slots=True)
class PairingWindowResult:
    processed: bool
    accepted: int = 0
```

Meaning:

```text
processed=False
    budget denied OR pairing model unavailable/recoverable failure

processed=True, accepted=0
    window successfully evaluated but no accepted pair

processed=True, accepted>0
    window successfully evaluated and accepted pairs
```

Do not encode this distinction in exceptions or negative integers.

---

## 5.2 `_process_window`

Change return type to `PairingWindowResult`.

Required outcomes:

### fewer than 2 messages

```text
mark window processed
return PairingWindowResult(True, 0)
```

### budget denied before model

```text
leave window pending
return PairingWindowResult(False, 0)
```

### model unavailable / timeout

```text
leave window pending
return PairingWindowResult(False, 0)
```

### model returns no usable pair

```text
mark window processed
return PairingWindowResult(True, 0)
```

### accepted pairs

```text
persist candidate audit rows
persist context_question
project evidence
mark old window processed after successful extraction attempt
return PairingWindowResult(True, accepted_count)
```

If one candidate projection fails:

- preserve candidate audit row;
- preserve `context_question`;
- set message `index_status=FAILED`;
- leave projection manifest failed;
- the window extraction itself counts as processed, because semantic candidate extraction succeeded and repair can fix the projection;
- do not repeatedly ask the LLM to rediscover the same pair on every future message.

Therefore projection failure is **not** equivalent to pairing-model failure.

---

## 5.3 `on_message`

Final logic:

```text
load incoming message
load pending window
if old window exists and is quiet:
    result = process old window
    if result.processed is False:
        return result.accepted
    old window is closed
    create a NEW window containing current activity
else:
    create or extend current window
```

The current incoming message MUST NOT be dropped merely because previous window produced zero pairs.

---

## 5.4 Preserve updated answer state

Do not reuse stale `answer` after setting context.

Use:

```python
updated_answer = replace(
    answer,
    context_question=question.text,
    index_status=IndexStatus.PENDING,
)
await self.messages.save(updated_answer)
```

Project using `updated_answer`.

Success:

```python
await self.messages.save(
    replace(
        updated_answer,
        index_status=IndexStatus.INDEXED,
        indexed_at=self.clock.now(),
    )
)
```

Failure:

```python
await self.messages.save(
    replace(
        updated_answer,
        index_status=IndexStatus.FAILED,
    )
)
```

The context question must remain in all cases.

---

## 5.5 Pairing tests

Mandatory:

1. one conversation:
   - Q at T0;
   - A at T0+30 seconds;
   - next message at T0+3 minutes;
   - old window is processed;
   - current third message starts new pending window.

2. same setup but LLM returns zero pairs:
   - old window marked processed;
   - third message still starts new window.

3. budget denied:
   - old window remains pending;
   - no model call;
   - current incoming event does not overwrite the old window.

4. model timeout:
   - old window remains pending.

5. projection failure:
   - candidate exists;
   - answer row retains `context_question`;
   - message index state is failed;
   - projection manifest is failed.

6. repair after projection failure:
   - projects `msg:<answer>`;
   - metadata includes original question;
   - message becomes indexed.

---

# 6. Phase D — correction approval projection semantics

Fix both:

```text
generic REST review decision
Telegram callback approval
```

Semantic Q&A approval and search projection are separate outcomes.

## 6.1 Application-level helper

Avoid duplicating exception handling in two adapters.

Add a small application/use-case method, preferably on `ReindexService`:

```python
async def try_reindex_qa_version(
    self,
    version_id: str,
) -> ProjectionAttempt:
    ...
```

with:

```python
@dataclass(frozen=True, slots=True)
class ProjectionAttempt:
    status: str   # "indexed" | "failed" | "not_current"
```

Do not use a generic Result framework.

Behavior:

```text
current + projection success -> indexed
superseded/not current       -> not_current
projection/model failure     -> failed
```

Projection state already records the failure durably.

Do not swallow programming errors unrelated to known projection/model failures.

---

## 6.2 Generic REST review decision

After semantic approval:

```python
version = await context.feedback.approve(...)
attempt = await context.reindex.try_reindex_qa_version(version.id)
```

Return HTTP 200 because semantic approval succeeded.

Response:

```json
{
  "status": "approved",
  "feedback_id": "...",
  "qa_item_id": "...",
  "qa_version_id": "...",
  "projection_status": "indexed|failed|not_current"
}
```

Do not 500 merely because embedding/Vectorize failed.

---

## 6.3 Telegram approval callback

After semantic approval:

```text
attempt projection
ack callback regardless of projection result
notify reporter/reviewer semantic approval succeeded
```

If projection failed, use one concise admin/reviewer warning such as:

```text
Correcció aprovada. La indexació ha fallat i queda pendent de reparació.
```

Do not tell ordinary group users about internal Vectorize details.

A failed projection is recoverable through:

```bash
kb index repair --limit 100
```

Do not automatically run repair.

---

## 6.4 Revert and seed renewal

Inspect these semantic operations too.

If they:

```text
commit SQL
then project
```

apply the same rule:

```text
semantic operation stays committed
projection failure returned separately
```

Do not broaden the task beyond those paths if they already handle this correctly.

Add tests only where required.

---

# 7. Phase E — callback acknowledgement

Audit every action produced by:

```text
callback_action(data)
```

For each recognized action, `answer_callback()` must be called exactly once.

Required cases:

```text
start:
    target missing
    feedback cannot start
    force reply succeeds
    force reply fails

approve_global:
    review missing
    unauthorized
    already resolved
    approval succeeds + projection succeeds
    approval succeeds + projection fails

approve_group:
    same as above

edit:
    review missing
    unauthorized
    prompt succeeds/fails

reject:
    review missing
    unauthorized
    already resolved
    success
```

For the specific current bug:

```python
feedback = await context.feedback.start(...)
if feedback is None:
    await context.transport.answer_callback(
        callback_id,
        "Aquesta resposta ja no es pot corregir.",
    )
    return "ignored"
```

Exact Catalan wording may differ, but it must be deterministic and user-safe.

Do not acknowledge malformed unrelated callback data that is not recognized as ours.

Add callback-count assertions to tests.

---

# 8. Phase F — background backlog contract

Change:

```python
class BackgroundBacklogRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=100)
```

Update docs/examples.

Add a request validation test:

```text
limit=100 => accepted
limit=101 => 422
```

Do not change budget policy.

---

# 9. Phase G — answer delivery retry regression

Do not redesign delivery.

Add integration coverage around the existing webhook path.

Use a fake Telegram client capable of:

```text
first send_answer call -> return None / fail
second call -> return external id
subsequent calls -> record if invoked
```

Test:

```text
1. POST synthetic addressed Telegram update.
2. Assert response is 503 or current documented retry-causing response.
3. Assert:
   - inbound message exists;
   - BotAnswer exists;
   - no delivery receipt.
4. POST SAME Telegram update again.
5. Assert:
   - response success;
   - generator/retrieval answer decision not repeated where deterministic durable replay is expected;
   - Telegram send called successfully;
   - exactly one delivery receipt exists.
6. POST SAME Telegram update a third time.
7. Assert:
   - Telegram send count did not increase;
   - no duplicate receipt.
```

The test must prove the persisted inbound event does not suppress needed delivery retry.

Do not fake this by calling `AnswerService` directly.

Hit `/telegram/webhook`.

---

# 10. Phase H — full synthetic Telegram local E2E

Create a new smoke/E2E test file:

```text
tests/smoke/test_local_telegram_full_flow.py
```

Mark:

```python
pytestmark = pytest.mark.e2e_local
```

Run only under:

```text
RUN_LOCAL_AI_E2E=1
```

The test uses:

```text
real local HTTP app
real SQLite
real NumPy vector store
real Ollama embedding model
real Ollama generation/pairing adapters where invoked
fake Telegram final transport/client
synthetic Telegram Updates
```

No real Telegram Bot API.

If current local app composition always constructs the real Telegram HTTP client, add exactly one local-test dependency injection mechanism so the local E2E can supply a fake `TelegramClient`.

Do not add a DI framework.

A constructor/factory optional argument is enough.

---

## 10.1 Mandatory two-space flow

Use spaces A and B bound to two synthetic Telegram group ids.

Sequence:

### Setup

1. start from isolated test SQLite state;
2. register/bind Telegram group A → space A;
3. register/bind Telegram group B → space B;
4. seed global:
   ```text
   Q = unique run-specific question
   A = Global V1 marker
   ```

### Telegram ask

5. POST synthetic `/ask <question>` in A;
6. fake Telegram client receives answer containing Global V1;
7. repeat in B;
8. B receives Global V1.

### Local correction in A

9. trigger correction callback on A answer;
10. submit proposal reply;
11. configure local reviewer for A;
12. verify local reviewer review UI/request excludes or server-denies global approval;
13. explicitly forge an `approve_global` callback as A local reviewer;
14. assert:
    - callback acknowledged;
    - global knowledge unchanged.
15. approve local correction;
16. ask again in A → local corrected marker;
17. ask in B → still Global V1.

### Global correction

18. global reviewer or admin approves Global V2;
19. ask in A → A still gets local override;
20. ask in B → B gets Global V2.

### Restart semantics

21. close/rebuild application context against the same SQLite file/volume;
22. ask A and B again;
23. results remain identical.

No Cloudflare resources touched.

Prefer seeded direct Q&A so this test remains deterministic and cheap.

---

# 11. Phase I — full local background pair E2E

Create:

```text
tests/smoke/test_local_background_pair_flow.py
```

Use unique run ids.

Setup:

```text
space A / Telegram group A
space B / Telegram group B
background listener enabled
```

Sequence in A:

1. send unaddressed factual question;
2. send nearby unaddressed clear answer;
3. wait/advance by sending third unaddressed text after quiet period;
4. verify previous window closes;
5. verify answer message persists:
   ```text
   context_question = original question
   index_status = indexed
   ```
6. verify vector:
   ```text
   id = msg:<answer id>
   kind = message_evidence
   scope = space A
   ```
7. send addressed semantically equivalent question in A;
8. assert retrieval/answer can cite/use the `msg:<answer>` evidence;
9. send equivalent addressed question in B;
10. assert B does not retrieve A message evidence.

Because real `gemma3:270m` pairing output may vary:

- choose extremely explicit short messages;
- assert schema/provenance/retrieval behavior;
- do not assert exact generated prose;
- if pairing output is still too nondeterministic, keep **real embedding/generation** but use a deterministic local PairingModel fake only in this test. Document this exception in the test. Prefer real Ollama pairing first.

Do not contact Cloudflare.

---

# 12. Phase J — remote safety and CI

## 12.1 Makefile guards

Change remote targets so they check, not set:

```make
@test "$(ALLOW_CLOUDFLARE_LIVE_TESTS)" = "1" || (...)
@test -n "$(BOT_BASE_URL)" || (...)
```

Then invoke:

```bash
uv run ...
```

without injecting:

```text
ALLOW_CLOUDFLARE_LIVE_TESTS=1
```

The environment value should already come from the human caller.

Apply to:

```text
eval-live
eval-live-reindex
seed-self-qa
smoke-cloudflare
```

If a target causes any remote mutation/AI work, apply the same pattern.

---

## 12.2 CLI guards

Keep `_require_live_allowed`.

Ensure it is called before any remote network call in:

```text
seed
group add/update
revert
reindex
index repair
smoke-cloudflare
set-webhook
delete-webhook
remote maintenance
```

For `delete-webhook`, explicit remote authorization is still required even though the target is Telegram rather than the Worker.

Do not infer safety from command name.

---

## 12.3 Eval guard

Keep:

```text
ALLOW_CLOUDFLARE_LIVE_TESTS=1
--base-url explicit
```

Do not auto-enable it in Make.

---

## 12.4 CI workflow

Add:

```yaml
name: ci

on:
  push:
  pull_request:

jobs:
  offline:
    runs-on: ubuntu-latest
    steps:
      - checkout
      - install uv
      - uv sync --frozen
      - make lint
      - make test
      - make test-integration
      - uv run python -m evals.run offline
```

Use the official/recommended `uv` setup action already standard in the ecosystem.

No credentials.

No service containers.

No Ollama.

No Docker.

---

# 13. Phase K — finish Telegram adapter boundary

This phase is structural cleanup only after all correctness tests pass.

Do not change external behavior.

## 13.1 Remove `MessageTransport`

Search the repository for:

```text
MessageTransport
```

If no non-Telegram channel-neutral use remains:

- delete `ports/transport.py`;
- update services that depended on it to use narrower channel-independent notifier ports if needed;
- where behavior is genuinely Telegram-specific, depend on `TelegramClient` only from adapter-layer code.

Application services MUST NOT import `adapters.telegram.client`.

If an application service currently needs to notify an admin in a channel-neutral way, use the existing `ports/notifier.py` or extend it narrowly with plain-text notification only.

Do not put buttons/ForceReply/callbacks in a core port.

---

## 13.2 Move Telegram orchestration

Move Telegram-specific orchestration helpers from:

```text
adapters/http/app.py
```

to:

```text
adapters/telegram/flow.py
```

The final `TelegramFlow` should own:

```text
normalize Update
resolve Telegram-bound space
reviewer Telegram commands
feedback Telegram callbacks
ForceReply correlation
Telegram delivery receipts
Telegram outbound UI behavior
```

It may call application services through `AppContext`.

Do not move generic application use cases into Telegram.

`adapters/telegram/routes.py` should authenticate webhook and call `TelegramFlow.handle()`.

`adapters/http/app.py` should primarily:

- create FastAPI app;
- register generic API routes;
- register Telegram route adapter;
- register internal maintenance routes.

Do not move generic `/v1/*` routes there.

---

# 14. Phase L — remove dead reviewer report system

Delete:

- `ReviewerReportService`;
- dead `render_report()` if not used by DailyReport;
- obsolete report state dependency used only by this legacy service;
- any old settings only used by it;
- tests only covering the no-op compatibility behavior.

Keep:

- `ReviewerEvent`;
- reviewer event repository;
- daily report aggregation of reviewer events if currently used.

Do not delete historical audit data support that the real DailyReport still consumes.

---

# 15. Phase M — neutral shared SQL repositories

This is a code ownership move, not a query rewrite.

## 15.1 Target structure

Final:

```text
src/knowledge_bot/infrastructure/sql/
├── __init__.py
├── protocol.py
└── repositories.py
```

`repositories.py` contains actual repository implementations.

Rename:

```text
D1MessageRepository
```

to:

```text
SqlMessageRepository
```

and similarly for all SQL repositories that use only the common `prepare/bind/run/first/batch` protocol.

Keep Cloudflare-only objects out of this module.

---

## 15.2 Cloudflare module

After extraction, `infrastructure/cloudflare/d1.py` should contain only the D1 execution adapter/binding or other genuinely Cloudflare-specific helpers.

Do not leave all shared repository SQL duplicated in both places.

---

## 15.3 Local module

`infrastructure/local/sqlite_repositories.py` should continue to implement the SQLite execution binding, not repository business logic.

Local composition imports:

```text
Sql*Repository
```

from `infrastructure/sql/repositories.py`.

Cloudflare composition does the same.

---

## 15.4 Tests

Existing D1/SQLite repository contract tests must still pass.

Add one architecture assertion:

```text
infrastructure/sql/repositories.py
must not import
knowledge_bot.infrastructure.cloudflare
```

and:

```text
infrastructure/local/*
must not import D1* repository classes
```

Do not add generic repository abstractions.

---

# 16. Phase N — privacy-safe logging

## 16.1 Configuration

Ensure only one module configures Loguru sinks.

At entrypoints:

```text
local_entry.py -> configure local DEBUG human-readable
entry.py       -> configure cloudflare INFO serialized JSON
```

Do not configure logging in FastAPI route registration.

---

## 16.2 Boundary logger helper

Use small structured calls such as:

```python
logger.bind(
    use_case="telegram_webhook",
    update_id=safe_id,
    space_id=space_id,
).info("webhook_processed")
```

Do not interpolate user text.

Add duration using monotonic timing at adapter/infrastructure boundary where useful.

---

## 16.3 Required events

At minimum:

```text
telegram_webhook_received
telegram_webhook_processed
question_answered
background_classified
projection_active
projection_failed
projection_repair_finished
reindex_batch_finished
model_call_finished
model_call_failed
daily_report_finished
```

Fields may include:

```text
request/update id
space id
answer mode
candidate/evidence counts
classification label
classification score
projection vector id
model name
duration_ms
error class
processed counts
```

Never raw user content.

---

## 16.4 Tests

Add architecture/privacy tests that source-scan or exercise logging helpers enough to prove:

- no Loguru import under `domain/`;
- no Loguru import under `application/`;
- production `log_content=True` still rejected;
- representative boundary log records do not include raw message or answer text.

Do not over-engineer logging tests.

---

# 17. Phase O — documentation reconciliation

Do this only after all code/tests pass.

## 17.1 Add missing binding plans

Add to repo:

```text
instructions/qa-telegram-bot-final-hardening-plan.md
instructions/qa-telegram-bot-one-pass-final-fix-plan.md
```

The second is this document.

Update `AGENTS.md`:

```text
current binding contract:
one-pass-final-fix-plan

previous:
final-hardening-plan

historical:
refactor-v2-spec
```

Do not leave ambiguous precedence.

---

## 17.2 Update session handoff

Rewrite:

```text
docs/session-handoff.md
```

Final content must include:

- current `main` base from which branch started;
- this final hardening branch name;
- summary of fixes;
- exact tests run and results;
- external/manual acceptance still outstanding;
- explicit statement that no remote work was run unless user authorized it;
- next human action.

Do not include stale branch history.

---

## 17.3 Update primary docs

Review and update:

```text
README.md
docs/architecture.md
docs/setup-local.md
docs/setup-cloudflare.md
docs/e2e-telegram.md
docs/development.md
docs/knowledge-base.md
docs/operations.md
data/seed/bot_self_qa.json
TODO.md
```

Required corrections:

- `kb reindex` documented as cleanup + bounded batches;
- projection repair documented before destructive rebuild;
- background backlog max 100;
- local synthetic Telegram E2E documented;
- real Telegram manual E2E remains separate;
- CI documented;
- remote commands require explicit flag and URL;
- no nonexistent binding-plan paths;
- no claims that all acceptance is complete unless it actually is;
- no stale reviewer-report behavior;
- no claim that Telegram-specific primitives are core abstractions.

---

## 17.4 TODO cleanup

Do not delete genuine future product ideas:

```text
better question detection
DM multi-space resolution
off/silent/active/proactive modes
Telethon future E2E
```

But update/remove items already completed.

Cloudflare deployment/smoke TODO must reflect actual current state accurately. Do not guess: use repository docs/history.

---

# 18. Final mandatory acceptance matrix

The agent MUST run all of these locally before declaring completion.

## 18.1 Static / offline

```bash
make format
make lint
make test
make test-integration
uv run python -m evals.run offline
```

All must pass.

---

## 18.2 Local runtime

```bash
make dev-bootstrap
make test-e2e-local
```

Must pass using current checkout.

No Cloudflare credentials required.

No Cloudflare resources touched.

---

## 18.3 Rebuild regression proof

Using test/fake/local database only, prove:

```text
cleanup = zero AI
N current records
→ each record embedded/projected exactly once
→ no duplicate full pass
```

Do NOT run a remote production reindex to prove this.

---

## 18.4 Required correctness checklist

Every box must be true:

### Reindex

- [ ] cleanup is separate from indexing;
- [ ] cleanup performs no embeddings;
- [ ] server reindex is bounded;
- [ ] CLI new run cleans once then batches;
- [ ] CLI resume skips cleanup;
- [ ] no corpus-wide duplicate embedding.

### Retrieval

- [ ] local same-canonical overrides global;
- [ ] unrelated strong global can outrank weak locals;
- [ ] authority breaks equal-similarity ties.

### Pairing

- [ ] zero-pair processed window closes successfully;
- [ ] third message after quiet period starts new window;
- [ ] model/budget failure leaves old window pending;
- [ ] context_question survives projection failure;
- [ ] failed pair projection can be repaired;
- [ ] repaired evidence remains ordinary `msg:*`.

### Correction

- [ ] semantic approval is atomic;
- [ ] projection failure does not undo approval;
- [ ] generic API returns `projection_status=failed` rather than 500;
- [ ] Telegram callback is acknowledged on projection failure;
- [ ] repair path can later index it.

### Telegram callback

- [ ] every recognized callback branch acknowledges exactly once.

### Delivery

- [ ] first Telegram answer delivery failure leaves answer durable;
- [ ] retry delivers stored answer;
- [ ] receipt created only after successful delivery;
- [ ] third identical update does not duplicate send.

### Background

- [ ] backlog max is 100.

### Local E2E

- [ ] synthetic Telegram two-space correction flow passes;
- [ ] forged local global-approval is denied;
- [ ] state survives context restart;
- [ ] local background pair E2E passes;
- [ ] A evidence is not visible in B.

### Remote safety

- [ ] Make does not self-set live authorization;
- [ ] CLI still checks authorization;
- [ ] remote URL explicit;
- [ ] no live command defaults to production.

### Architecture

- [ ] no Telegram ForceReply/callback mechanics in core transport ports;
- [ ] generic HTTP app no longer owns most Telegram flow;
- [ ] dead ReviewerReportService deleted;
- [ ] local persistence does not import Cloudflare repository classes;
- [ ] shared SQL repository implementations live under `infrastructure/sql`.

### CI/logging/docs

- [ ] offline GitHub Actions workflow exists;
- [ ] no CI remote credentials/services;
- [ ] privacy-safe boundary logging implemented;
- [ ] current binding plan files exist;
- [ ] AGENTS precedence is correct;
- [ ] session handoff is current;
- [ ] docs match actual commands;
- [ ] self-Q&A matches actual behavior.

---

# 19. Explicitly out of scope

Do NOT implement any of these in this pass:

- Telethon;
- Playwright;
- browser automation;
- WhatsApp;
- user DM group-membership discovery;
- off/silent/active/proactive modes;
- better semantic classifier research;
- new curator product workflow;
- new UI;
- authentication redesign;
- PostgreSQL;
- SQLAlchemy;
- performance optimization beyond removing duplicate reindex;
- new AI models;
- model benchmarking;
- threshold tuning;
- media support.

They may remain in `TODO.md`.

---

# 20. Final agent handoff format

When the agent finishes, it must return a concise report in exactly this structure:

```markdown
# Final hardening implementation

## Branch
`fix/final-hardening-pass`

## Commits
- ...
- ...

## Fixed
- F1 ...
- F2 ...
...
- F19 ...

## Tests run
- `make lint` — PASS
- `make test` — PASS, N tests
- `make test-integration` — PASS, N tests
- `uv run python -m evals.run offline` — PASS
- `make test-e2e-local` — PASS, N tests

## Remote operations
None performed.
```

If remote operations were explicitly authorized and performed, list each command and result instead of claiming none.

Then:

```markdown
## Remaining manual acceptance
- real Telegram human two-group acceptance
- any explicitly deferred external check
```

Do not write “complete” if a required local gate failed.

Do not merge to `main` automatically unless the human explicitly asked the agent to merge.

---

# 21. Final instruction

This task is a **hardening completion pass**, not another refactor.

Prefer the smallest explicit change that makes the specified invariant true.

For every defect:

```text
write regression
→ implement narrow fix
→ run gates
```

Do not “clean up while here” outside Sections 3–17.

The most important safety property in this pass is:

```text
NEVER turn a bounded repair/reindex operation into an implicit full remote AI job.
```

The most important correctness properties are:

```text
semantic SQL commits survive search-projection failure
local knowledge overrides only the same canonical global question
temporal pair state remains rebuildable
Telegram retries cannot lose an already-persisted answer
```

The most important acceptance property is:

```text
the complete functional cycle is reproducible locally through synthetic Telegram,
without Cloudflare and without real Telegram network access.
```

Implement exactly that.
