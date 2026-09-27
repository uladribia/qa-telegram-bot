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

`make dev-bootstrap` creates the `knowledge-bot-dev` network and the `knowledge-bot-data` and `knowledge-bot-ollama-data` volumes, starts Ollama, pulls the two local models, builds the app image, applies migrations, and starts the app.

SQLite and Ollama models persist in named volumes. `make dev-reset CONFIRM=1` removes only the local SQLite volume; the Ollama model volume is preserved. `make dev-down` stops containers without deleting volumes.

All local AI work is explicit. `make test` and `make test-integration` do not call Ollama. `make test-e2e-local` rebuilds the current app image and runs synthetic Telegram, real SQLite/NumPy, and real Ollama smoke flows inside the container. No real Telegram token or network is used.

## Traces

The local runtime exports request traces to Logfire (EU region, project `oleguer-sagarra/qa-telegram`). The send-only write token lives in the gitignored `.logfire/logfire_credentials.json`; commit `.logfire/.gitignore` and never the credentials file. A container or any runtime without that file needs `LOGFIRE_TOKEN` in its environment. Set `KB_LOGFIRE_SEND_TO_LOGFIRE=false` in `.env.local` to keep spans on the machine. Details and the query command are in [operations.md](operations.md#traces-logfire).
