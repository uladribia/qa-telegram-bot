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

All local AI work is explicit. `make test` and `make test-integration` do not call Ollama. `make test-e2e-local` rebuilds the current app image and runs the local AI smoke tests inside the container.
