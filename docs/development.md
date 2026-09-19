# Development

How the code is organised, how to change it, and what must pass before it lands.

The binding rules live in [`../AGENTS.md`](../AGENTS.md). This file is the
practical companion: where things go and what to run.

---

## Layout and the dependency rule

```text
src/knowledge_bot/
├── domain/          entities, value objects, enums, policies — no I/O, no frameworks
├── application/     use cases and orchestration — domain + ports only
├── ports/           Protocol interfaces (repositories, vector store, embedder,
│                    generator, transport, clock, budget)
├── contracts/       Pydantic DTOs at external boundaries
├── adapters/        inbound/ (webhook, importers), outbound/ (Telegram)
├── infrastructure/  concrete externals (D1, Vectorize, Workers AI, settings, logs)
├── entry.py         Worker entrypoint
└── cli.py           Typer CLI
tests/{unit,integration,architecture}/
```

The rule that matters most, and which `tests/architecture/` enforces:

```
domain/ and application/  →  must NOT import fastapi, workers, Telegram libs,
                             D1/Vectorize bindings, HTTP clients, Loguru,
                             or infrastructure/
```

`application/` depends on `ports/` Protocols, never on concrete adapters. Convert
external payloads to Pydantic at the adapter boundary, as early as possible.

---

## Everyday workflow

```bash
git switch -c feat/thing     # never commit to main
# ... change code ...
make all                     # lint + fast tests
make test-integration        # when you touched a flow or an adapter
git commit -m ":sparkles: Add thing"
git switch main && git merge --no-ff feat/thing
git branch -d feat/thing
```

Commit messages use a **gitmoji** prefix (`:sparkles:`, `:bug:`, `:memo:`,
`:white_check_mark:`, `:zap:`, `:goal_net:`).

---

## Which gate to run

Pick the smallest that covers the change. Do not default to the heaviest.

| Change | Run |
|---|---|
| Pure logic, docs, config | `make test` |
| A use case, flow or adapter behaviour | `make test-integration` |
| `entry.py`, routes, bindings, Dockerfile, `wrangler.jsonc` | `make smoke` |
| Embeddings, provenance, index shape | `make reindex` |
| Before merging to `main` | `make lint` + the smallest tier that covers it |

`make all` = lint + fast tests. `make test` is the fast tier and must stay fast: no
sleeps, no network, no subprocesses, no Docker.

**Do not run `make smoke` on every change** — it builds and boots the Worker and
takes minutes. Milestone boundaries and runtime-affecting changes only.

---

## Tests

Tiers are directory-based and marked automatically by `tests/conftest.py`:

- `tests/unit/` — pure logic, no I/O.
- `tests/architecture/` — import-boundary checks via stdlib AST.
- `tests/integration/` — full use-case flows wired to in-memory fakes.
- `tests/smoke/` — reserved; the real runtime gate is `make smoke`.

Rules:

- One test file per module under test, named after it.
- **Never** call real Telegram, Cloudflare, D1, Vectorize or models from unit or
  integration tests. Fakes live in `tests/fakes/`.
- Cover idempotency for each ingest path: the same inbound event twice must not
  duplicate state.

Tests prove the code works. **Evals prove the answers are good** — don't blur that.

---

## Evals

```bash
uv run python -m evals.run offline     # structural; free, run often
make eval-live                         # real model calls; costs quota
```

`evals/*.yaml` hold the cases; `evals/run.py` executes them. Offline checks that
the files are well formed. Live checks the deployed bot's real behaviour: retrieval
recall, answer correctness, citation validity, and abstention.

The live answer suite runs the LLM judge as a **second, conditional call** — only
for cases that already passed every deterministic check. Keep it that way; the
judge is the largest quota consumer in the suite.

Eval thresholds in the plan are acceptance criteria, not suggestions.

---

## Docs

Documentation must never diverge from the code in a merge. If behaviour, commands,
configuration or architecture changed, update the affected file in the same branch:

- `README.md` — entry point and commands
- `docs/` — this folder
- the plan in `instructions/` — it is the binding spec
- docstrings — Google style, on every module and function

Every `.py` file starts with `# SPDX-License-Identifier: MIT`.

---

## Traps worth knowing

- **`entry.py` builds context from `request.scope["env"]`.** The top-level
  `workers.env` is empty at import time; the context is cached per isolate.
- **D1 is the source of truth; Vectorize is rebuildable.** Never store anything
  that exists only in the index.
- **Vectorize needs metadata indexes** on `kind` and `status` or filtered queries
  silently return nothing.
- **Async everywhere** for I/O. No blocking calls in request paths.
- **No backwards compatibility.** This is a prototype: change the call sites,
  tests and docs together rather than adding a compatibility path.
- **Metering must never break an answer.** The budget wrappers swallow their own
  failures on purpose.
