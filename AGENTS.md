# AGENTS.md

Instructions for coding agents (and humans) working in this repository.
This file describes **how** to write code here, not **what** to build.

The binding product/implementation plan lives in
[`instructions/pla_prototip_bot_telegram_bhc_v3.md`](instructions/pla_prototip_bot_telegram_bhc_v3.md).
Read it before starting any work. If this file and the plan disagree, stop and ask;
never silently pick one.

---

## 0. Harness tools (strict)

Use the harness tools for all file and repository work: `read` to inspect files,
`edit` for targeted changes, `write` for new or fully rewritten files, and
`bash`/`grep`/`find` for search and commands. Never shell out to Python (or any
other language) to read, write, patch, move, or reformat files: no
`python -c "open(...)"`, no heredoc patch scripts, no ad-hoc rewriting scripts.
Use the right standard tool for the job (`ruff`, `git`, `gh`, `make`). Python
execution is for the product code and its tests, not for editing the repository.

## 1. Non-negotiables

- **Use the harness tools for all file operations** (see §0). Never edit files
  via Python or shell scripts.
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
| Dev environment | Docker (dev/CI only, no Compose) |

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
make format           # uv run ruff format . && uv run ruff check --fix .
make lint             # uv run ruff format --check . && uv run ruff check . && uv run ty check
make test             # fast tier: unit + architecture, no external services
make test-integration # in-process flows with in-memory fakes
make test-all         # every test tier
make smoke            # build the dev image, run the Worker, check /healthz
make all              # lint then the fast test tier
```

Pick the smallest command that covers the change:

| Change | Run |
|---|---|
| Pure logic, docs, config | `make test` |
| Use case, flow, or adapter behaviour | `make test-integration` |
| `entry.py`, routes, bindings, Dockerfile, `wrangler.jsonc` | `make smoke` |
| Before merging to `main` | `make lint` plus the smallest tier that covers the change |

Do not run `make smoke` on every change: it builds and boots the Worker and takes
minutes. Reserve it for milestone boundaries and runtime-affecting changes.

`uv run` is the fast, canonical path. Docker is a fidelity check via direct
`docker build`/`docker run` (there is no Docker Compose). Do not add a second
set of commands to scripts or docs.

A task is not done until the smallest relevant tier passes and `make lint` exits 0.

## 6. Testing

Tiers are directory-based and marked automatically by `tests/conftest.py`:

- `tests/unit/` — pure logic, no I/O, no network. Keep the whole tier fast.
- `tests/architecture/` — import-boundary checks (stdlib AST). Fast.
- `tests/integration/` — full use-case flows wired to in-memory fakes. No network.
- `tests/smoke/` — reserved for runtime checks; the actual runtime gate is `make smoke`.

Rules:

- One test file per module under test, named after that module.
- **Never** call real Telegram, Cloudflare, D1, Vectorize, or models from unit or
  integration tests. Mock or fake external services. Fakes live in `tests/fakes/`
  (in-memory repositories, fake embedder/vector store/generator/transport).
- Keep the fast tier genuinely fast: no sleeps, no network, no subprocesses, no
  Docker. If a test needs any of those, it belongs in `integration` or `smoke`.
- Prefer deterministic assertions over timing- or ordering-dependent ones.
- Answer quality is verified by **evals**, not unit tests. Eval thresholds in the
  plan are acceptance criteria. Keep the distinction: tests prove the code works;
  evals prove the answers are good.
- Idempotency matters: processing the same inbound event twice must not duplicate
  state. Cover it in integration tests for each ingest path.

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
- Every merge to `main` must carry the documentation it needs. Documentation must
  never diverge from the code on a final merge: if behaviour, commands,
  configuration, or architecture changed, update the affected docs (README,
  docstrings, plan notes) in the same branch before merging.
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
4. Confirm the docs reflect the change before merging to `main`.

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
