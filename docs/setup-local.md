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

## Listener decisions

The local stack takes each unaddressed message's intent from the local
System-One decision service: one `POST /v1/systemone` request per message,
served by the same Ollama container. This is about **fidelity**, not quality —
the local model is a testing stand-in, and its numbers live in
`reports/decision-service.md`. Production runs the linear classifier and adds
no remote decision adapter.

Two switches in `.env.local` control the risk:

```text
DECISION_INCLUDE_RELEVANCE=false   the service classifies; the deterministic
                                   pairing policy still chooses the question
DECISION_FALLBACK_TO_BASELINE=true answer from the linear classifier when the
                                   service is unreachable, logging the reason
```

```bash
make decision-smoke    # runtime gate: does the service answer one canonical request?
make decision-eval     # informational scoring of the local model
```

`make dev-up` verifies the decision service answers before starting the app and
never downloads a model. If it does not answer, it stops and tells you to run
`make dev-bootstrap`. Troubleshooting is in
[operations.md](operations.md#local-system-one-decision-service).

SQLite and Ollama models persist in named volumes. `make dev-reset CONFIRM=1` removes only the local SQLite volume; the Ollama model volume is preserved. `make dev-down` stops containers without deleting volumes.

All local AI work is explicit. `make test` and `make test-integration` do not call Ollama. `make test-e2e-local` rebuilds the current app image and runs synthetic Telegram, real SQLite/NumPy, and real Ollama smoke flows inside the container. No real Telegram token or network is used.

## Traces

The local runtime exports request traces and content to Logfire (EU region, project `oleguer-sagarra/qa-telegram`, `environment=local`). A token is optional: the app runs normally without one and spans stay local. On a developer machine the send-only token is picked up from the gitignored `.logfire/logfire_credentials.json`; inside the container there is no such file, so set `LOGFIRE_TOKEN` in `.env.local`. Commit `.logfire/.gitignore`, never the credentials file. Set `KB_LOGFIRE_SEND_TO_LOGFIRE=false` to keep every span on the machine.

Message text, sender identity, prompts, and answers **are** exported while testing (`KB_LOGFIRE_CAPTURE_CONTENT`, default `true`), so a whole flow can be reconstructed; tokens, webhook secrets, and API keys never are. Details, the event table, and the query command are in [operations.md](operations.md#traces-logfire).
