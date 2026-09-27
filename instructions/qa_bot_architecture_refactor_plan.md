# Refactor plan — canonical API + service adapters

## Goal

Refactor the repository structure so the architecture clearly separates:

- canonical FastAPI API
- service-specific adapters such as Telegram
- shared application/business logic
- domain models/rules
- AI/provider infrastructure
- persistence

This is primarily a **structural refactor**. Preserve existing behaviour.

Do **not** introduce speculative abstraction layers or an HTTP client between adapters and the canonical API yet.

## Important: concurrent work is happening

Other agents may modify the repository before or during implementation.

Before editing:

1. Inspect the current repository tree and relevant files.
2. Compare current state with this plan.
3. Preserve changes already made that satisfy the intended architecture.
4. Do not blindly apply paths or code assumptions from this document.
5. Avoid reverting unrelated changes.
6. Prefer moving/refactoring existing code over rewriting it.
7. Run the existing tests after each meaningful structural move.

The architecture below is the target; exact filenames may differ if the repository has evolved.

---

# Target structure

Aim approximately for:

```text
src/knowledge_bot/
├── api/
│   ├── app.py
│   └── routes/
│       ├── questions.py
│       ├── feedback.py
│       ├── reviews.py
│       └── ...
│
├── adapters/
│   ├── telegram/
│   │   ├── routes.py
│   │   ├── flow.py
│   │   ├── normalize.py
│   │   ├── models.py
│   │   └── client.py
│   └── ...
│
├── models/
│   ├── common.py
│   ├── messages.py
│   ├── questions.py
│   └── feedback.py
│
├── application/
├── domain/
├── ports/
└── infrastructure/
```

Do not force this exact tree where the current code suggests a simpler equivalent.

---

# Architectural rules

## 1. `api/` owns FastAPI

FastAPI itself is not an adapter.

Move the canonical FastAPI application and generic HTTP routes out of:

```text
adapters/http/
```

and into:

```text
api/
```

The FastAPI app factory should eventually be boring:

```text
create app
register canonical API routes
register enabled adapter routes
return app
```

Do not leave business logic in `api/app.py`.

---

## 2. Canonical API stays channel-independent

Canonical routes should remain things such as:

```text
POST /v1/questions
POST /v1/feedback
POST /v1/reviews/...
```

They should invoke shared application services.

They must not contain Telegram-specific parsing, rendering, callbacks or delivery logic.

---

## 3. Telegram owns everything Telegram-specific

The Telegram entrypoint should become:

```text
POST /adapters/telegram/webhook
```

Move Telegram orchestration out of the generic FastAPI app.

This includes, where applicable:

- Telegram webhook validation
- Telegram update parsing
- update normalization
- callbacks
- Telegram commands
- force-reply handling
- Telegram-specific reviewer interactions
- Telegram message delivery
- Telegram-specific identity conversion
- Telegram-specific interaction state

Candidate functions currently living in the HTTP app should be reviewed and moved when they are Telegram-specific, e.g.:

```text
_handle_telegram_update
_handle_message
_handle_callback
_handle_feedback_reply
_handle_reviewer_command
_resolve_message_space
Telegram-specific review delivery
```

Do not mechanically move a function if it actually represents reusable application behaviour. Extract reusable behaviour into `application/` only when it is genuinely channel-independent.

---

## 4. Do not add an HTTP abstraction yet

For now:

```text
/adapters/telegram/webhook
        ↓
Telegram adapter
        ↓
application services
```

and:

```text
/v1/questions
        ↓
canonical API
        ↓
same application services
```

Both use the application layer directly.

Do **not** add:

- CanonicalBackendClient
- HTTP service interfaces
- adapter-to-API HTTP calls
- new ports purely anticipating a future deployment split

If adapters are later deployed independently, the adapter can be changed to call `/v1/*` over HTTPS then.

The current refactor should merely make that future split straightforward.

---

# Models

## 5. Centralize shared models where useful

Review the current `contracts/`, `domain/`, and adapter-specific models.

Shared cross-channel request/data models should have one obvious home, preferably:

```text
models/
```

Examples:

```text
QuestionRequest
QuestionResponse
NormalizedMessage
AnswerSource
feedback request/response models
```

Do not duplicate equivalent models for API, Telegram and persistence.

## 6. Keep service payload models inside adapters

Examples:

```text
TelegramUpdate
TelegramCallback
Telegram-specific webhook structures
```

remain under:

```text
adapters/telegram/
```

Do the same for future WhatsApp-specific payloads.

## 7. Preserve domain entities

Do not collapse domain concepts merely to reduce files.

`domain/` remains the home for concepts/rules intrinsic to the system, independent of API/provider/channel.

Examples:

```text
QAItem
QAVersion
Message
Feedback
Space
Reviewer
AnswerMode
policies
domain errors
```

If moving existing shared models to `models/` would blur the domain boundary, leave domain entities where they are.

The objective is clearer ownership, not file churn.

---

# Persistence

## 8. Keep raw SQL and explicit mapping

Do not introduce SQLAlchemy or an ORM.

Preserve the current pattern:

```text
domain/shared model
        ↕ explicit row mapping
repository
        ↕ raw SQL
SQLite / D1
```

Continue reusing canonical/domain models where the DB representation matches them.

Do not add parallel models such as:

```text
DBMessage
SQLMessage
MessageRow
```

unless there is an actual representation mismatch that requires one.

---

# AI

## 9. Preserve current AI separation

Keep the existing conceptual split:

```text
application/
    decides when/how AI is used

ports/
    generator/embedder/vector-store contracts

infrastructure/
    Ollama / Workers AI / Vectorize implementations
```

Do not move provider-specific AI code into the API or adapters.

Do not change model behaviour as part of this refactor.

---

# AppContext cleanup

## 10. Reduce Telegram leakage from shared context

Inspect `AppContext`.

Currently Telegram-specific dependencies such as `TelegramIdentity` and `TelegramClient` may leak into what is otherwise shared application composition.

Refactor these only as far as naturally required by the structural cleanup.

Desired direction:

```text
shared application context
    → shared services/dependencies

Telegram adapter
    → Telegram-specific identity/client/delivery
```

However, **do not invent a large transport abstraction just to eliminate two types**.

If fully removing those dependencies requires speculative interfaces or substantial behavioural changes, leave a small documented TODO and keep the refactor minimal.

---

# Route organization

Canonical:

```text
/v1/questions
/v1/feedback
/v1/reviews/...
```

Adapter-specific:

```text
/adapters/telegram/webhook
```

Internal/admin routes can remain clearly separate, e.g.:

```text
/internal/...
```

Do not rename unrelated internal endpoints unless necessary.

---

# Implementation sequence

1. Inspect current branch and recent changes.
2. Run tests and establish baseline.
3. Create `api/` package.
4. Move FastAPI app factory from `adapters/http` to `api`.
5. Move generic `/v1/*` routes into `api/routes/`.
6. Update imports only; verify behaviour.
7. Change Telegram webhook path to `/adapters/telegram/webhook`.
8. Move Telegram-specific orchestration out of `api/app.py`.
9. Consolidate Telegram code under `adapters/telegram/`.
10. Review shared vs Telegram-specific models.
11. Centralize only clearly shared models.
12. Review `AppContext` for obvious Telegram leakage and simplify where low-risk.
13. Delete obsolete files/import paths.
14. Run formatting, linting, typing and full tests.
15. Run existing integration/E2E tests for Telegram and canonical `/v1/*` routes.

---

# Non-goals

Do NOT:

- redesign answer/retrieval logic
- change classifier behaviour
- change embedding or LLM models
- modify eval semantics
- add an ORM
- add HTTP calls between local components
- create microservices
- create speculative interfaces
- rename every model for consistency
- rewrite functioning repositories
- modify unrelated Cloudflare/local parity work

Keep this a narrow architectural cleanup.

---

# Acceptance criteria

The work is complete when:

1. There is no `adapters/http` concept for the canonical FastAPI API.
2. FastAPI application composition lives under `api/`.
3. `/v1/*` routes are service/channel independent.
4. Telegram webhook is exposed as:

   ```text
   /adapters/telegram/webhook
   ```

5. Telegram-specific orchestration is under `adapters/telegram/`, not mixed into the generic FastAPI app.
6. Both the canonical API and Telegram adapter invoke the same application services directly.
7. No new internal HTTP layer exists.
8. Shared models are not unnecessarily duplicated.
9. Telegram-specific payload models remain Telegram-specific.
10. Persistence remains raw SQL with explicit mappings.
11. Existing AI architecture remains unchanged.
12. Existing behaviour and tests continue to pass.
13. `api/app.py` is substantially smaller and primarily performs composition/routing.
14. No unrelated concurrent-agent work has been overwritten.

## Final report

At completion, report only:

- resulting high-level tree
- files moved/created/deleted
- any intentional deviations from this plan due to newer repo changes
- any remaining Telegram leakage that was deliberately not abstracted
- test/lint/type-check results
- any behavioural change, if unavoidable

---

**Key implementation instruction:** refactor toward the target from whatever the repo looks like when you start; do not blindly execute a stale file-move checklist.
