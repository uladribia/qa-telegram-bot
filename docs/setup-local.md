# Local setup

The canonical local runtime uses Git, Docker, and `make` only. Host Python, Ollama, Cloudflare credentials, D1, Vectorize, and Workers AI are not required.

```bash
cp .env.local.example .env.local
make dev-bootstrap
make dev-up
curl http://localhost:8000/readyz
make dev-seed
make test-e2e-local
```

`make dev-bootstrap` creates the `knowledge-bot-dev` network and the `knowledge-bot-data` and `knowledge-bot-ollama-data` volumes, starts Ollama (image `0.35.1`, pinned in `scripts/local-dev.sh`, because the `/v1/systemone` route exists from 0.35), pulls the three local models — `embeddinggemma` for embeddings, `gemma3:270m` for generation, `tev1:0.8b` for System-One decisions — builds the app image, applies migrations, and starts the app.

## Decisions: the local runtime mirrors production

`DECISION_BACKEND=systemone` is the default everywhere, and the local stack runs
the same code path with the same settings as the Worker:

```
local   DECISION_MODEL=tev1:0.8b                  served by Ollama on /v1/systemone
prod    DECISION_MODEL=@cf/cloudflare/clef-flash   served by the Workers AI binding
```

Same transport, same request shapes, same thresholds, same fallback. The local
model is a **stand-in**: it lets the listener, the answer gate and the degraded
paths be exercised end to end without spending anything. Never read a local
number as a quality measurement — `make decision-eval` measures the deployed
model, and `reports/clef-answer-decisions.md` holds the result.

Three settings define what the decision model may do, and all three are on in
both environments:

```text
DECISION_BACKEND=systemone          decide with the model; `baseline` is the rollback
DECISION_INCLUDE_RELEVANCE=false    pairing stays with the deterministic policy
DECISION_ANSWER_PATH=true           sufficiency and evidence selection after retrieval
DECISION_FALLBACK_TO_BASELINE=true  a decision-service failure degrades to the floor
```

`make dev-up` checks that the decision service answers before starting the app
and never downloads a model. If it does not answer, it stops and points at
`make dev-bootstrap`. Troubleshooting and the rollback are in
[operations.md](operations.md#decisions-and-rollback).

## Traces

The local runtime exports request traces and content to Logfire (EU region, project `oleguer-sagarra/qa-telegram`, `environment=local`). A token is optional: the app runs normally without one and spans stay local. On a developer machine the send-only token is picked up from the gitignored `.logfire/logfire_credentials.json`; inside the container there is no such file, so set `LOGFIRE_TOKEN` in `.env.local`. Commit `.logfire/.gitignore`, never the credentials file. Set `KB_LOGFIRE_SEND_TO_LOGFIRE=false` to keep every span on the machine.

Message text, sender identity, prompts, and answers **are** exported while testing (`KB_LOGFIRE_CAPTURE_CONTENT`, default `true`), so a whole flow can be reconstructed; tokens, webhook secrets, and API keys never are. Details, the event table, and the query command are in [operations.md](operations.md#traces-logfire).
