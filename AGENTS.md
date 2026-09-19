# AGENTS.md

Instructions for coding agents (and humans) working in this repository.
This file describes **how** to write code here, not **what** to build.

The binding product/implementation plan lives in
[`instructions/pla_prototip_bot_telegram_bhc_v3.md`](instructions/pla_prototip_bot_telegram_bhc_v3.md).
Read it before starting any work. If this file and the plan disagree, stop and ask;
never silently pick one.

---

## 1. Non-negotiables

- **Zero cost.** Only the models listed in `ALLOWED_AI_MODELS` may be called.
  No paid fallback, no alternative provider. On quota exhaustion, degrade safely
  and never lose the inbound event.
- **Dependency rule.** `domain` and `application` never import transport,
  framework, or infrastructure code (see §3).
- **D1 is the source of truth.** Any vector/search index is derived and must be
  rebuildable from D1. Never store data that exists only in the index.
- **No PII, no raw message text in logs** (see §8).
- **Tests are offline and deterministic.** No live network, API, or model calls
  in tests; fakes live in tests.
- **v1 processes no media.** Attachments are metadata only.
- **Never commit secrets.** `.env` stays ignored; `.env.example` documents keys.
- **Never commit directly to `main`.** Always work on a branch (see §9).

## 2. Stack (locked)

| Concern | Tool |
|---|---|
| Language / runtime | Python 3.13 |
| Dependencies, env, run | `uv` |
| HTTP | FastAPI |
| Validation / DTOs / settings | Pydantic v2 |
| CLIs | Typer |
| Logging | Loguru |
| Lint + format | Ruff |
| Type checking | `ty` |
| Tests | `pytest` |
| Worker runtime / deploy | Cloudflare Python Workers, `pywrangler` |
| Dev environment | Docker + Docker Compose (dev/CI only) |

Do not add a framework or dependency that is not in the plan without a documented
reason. Reach for the standard library first. Prefer the existing stack.

## 3. Layout and dependency rule

```text
src/knowledge_bot/
├── domain/         entities, value objects, enums, policies (no I/O, no frameworks)
├── application/    use cases and orchestration (depends on domain + ports only)
├── ports/          Protocol interfaces (repositories, vector store, classifier,
│                   generator, transport, media, clock)
├── contracts/      Pydantic DTOs at external boundaries
├── adapters/       inbound/ (webhooks, importers) and outbound/ (transport, media)
├── infrastructure/ concrete externals (D1, Vectorize, Workers AI, logging, settings)
├── entry.py        Worker / ASGI entrypoint
└── cli.py          Typer CLI
tests/{unit,integration,architecture}/
```

Rules:

- `domain/` and `application/` must not import `fastapi`, `workers`, Telegram
  libraries, D1/Vectorize bindings, HTTP clients, Loguru, or `infrastructure/`.
- `application/` depends on `ports/` Protocols, never on concrete adapters.
- Convert external payloads to Pydantic DTOs at the adapter boundary, as early as
  possible.
- No SQL in use cases. No prompts in HTTP routes. No channel-specific branching in
  the core beyond explicit domain provenance policies.
- One deployable: one Worker, one D1 database, one vector index.

## 4. Code conventions

- Full type annotations on every function and method. Avoid `Any`; it is allowed
  only for raw external payloads, and must be validated into Pydantic immediately.
  Do not add `from __future__ import annotations`.
- Pydantic v2 at all boundaries (DTOs, settings, model-output validation). Domain
  entities may be stdlib dataclasses/enums when that is clearer.
- Google-style docstrings on every module and every function/method. Describe
  purpose; do not restate the signature. No `"""."""`, no authorship/date headers.
- Every `.py` file starts with `# SPDX-License-Identifier: MIT`.
- English for identifiers, comments, docstrings, and log messages. User-facing
  strings follow the plan's language rules.
- PEP8 naming; explicit names, no cryptic abbreviations.
- `async` for all I/O. No blocking calls in request paths.
- Raise domain/application exceptions; adapters translate them to transport
  responses. `logger.exception` only at boundaries.

## 5. Quality gates

Always use these commands; do not invent ad-hoc equivalents:

```bash
make format   # uv run ruff format . && uv run ruff check --fix .
make lint     # uv run ruff format --check . && uv run ruff check . && uv run ty check
make test     # uv run pytest
make all      # lint then test
```

`uv run` is the fast, canonical path. Docker
(`docker compose run --rm app ...`) is only a fidelity check for the Worker
runtime. Do not add a second set of commands to scripts or docs.

A task is not done until `make all` exits 0.

## 6. Testing

- Layout: `tests/unit/`, `tests/integration/`, `tests/architecture/`.
- One test file per module under test, named after that module.
- Unit tests cover pure logic with no I/O. Integration tests cover full use-case
  flows using in-memory repositories and fake ports (embedder, vector store,
  generator, transport). Architecture tests assert the import boundaries of §3
  (stdlib AST is enough; no heavy tooling).
- Never call real Telegram, Cloudflare, or models from tests.
- Answer quality is verified by **evals**, not unit tests. Eval thresholds in the
  plan are acceptance criteria. Keep the distinction: tests prove the code works;
  evals prove the answers are good.
- Determinism and idempotency matter: processing the same inbound event twice must
  not duplicate state.

## 7. Configuration, secrets, cost

- Settings are Pydantic settings read from the environment. Document every key in
  `.env.example`; real values live only in the ignored `.env`.
- Add a `.env.example` entry whenever you add a setting.
- Enforce `ALLOWED_AI_MODELS` in code and raise a configuration error for anything
  outside it. Never hardcode tokens, chat IDs, thresholds, or model names outside
  settings.

## 8. Logging and privacy

- Configure Loguru in exactly one module; no sinks configured elsewhere.
- Development: human-readable, `DEBUG`. Production: structured JSON to
  stdout/stderr only. Never write log files.
- Use contextual fields (`request_id`, `conversation_id`, `use_case`,
  `duration_ms`, ...) instead of string interpolation.
- Never log raw message text, answers, sender names/usernames/phone numbers, raw
  payloads, or full prompts. Log lengths, hashes, counts, similarities, model
  names, and decisions instead.
- Never send data to any AI service other than the allowed models.

## 9. Git and GitHub workflow

- `main` is integration-only: **no direct commits, ever**. Branch for every change.
- Branch naming: `<issue>-<short-slug>` when an issue exists, otherwise
  `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Commits use a **gitmoji** prefix, for example:
  `:sparkles: Add question classifier`,
  `:bug: Fix webhook idempotency`,
  `:white_check_mark: Add correction-flow test`,
  `:memo: Document logging rules`.
- Integrate by merging the branch into `main` (`--no-ff`). A formal PR is optional.
  Never force-push `main`.
- Use the `gh` CLI for repository operations. The repository is private.
- Do not commit generated data, raw exports, caches, or `data/raw/*`.

## 10. Agent self-test loop

After every change:

1. Run `make all`.
2. If it fails, fix the root cause and re-run. Never disable, skip, or weaken a check.
3. Repeat at most 3 times. If it still fails, stop and report exactly what you tried
   and the error output.

Never mark work complete while the gate is red.

## 11. Do not

- Add frameworks, services, or dependencies beyond the plan (no LangChain, no
  extra databases, no queues, no auth layer, no web frontend).
- Turn a port into a deployed service, or a module into a microservice.
- Process media, images, or audio in v1.
- Add paid or alternative AI fallbacks.
- Put SQL, HTTP calls, or channel callbacks in `domain/` or `application/`.
- Use generated bot output as evidence unless it became a versioned Q&A record
  with valid provenance.
- Mutate history destructively: corrections are new versions, never `UPDATE`s.
