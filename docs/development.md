# Development

The canonical local mode is SQLite + NumPy + Ollama inside Docker. Host Python, uv, Ollama, and Cloudflare credentials are not needed for the documented local path.

## Test ladder

```bash
make lint
make test
make test-integration
```

`make test` is unit plus architecture and makes no network calls. `make test-integration` uses shared in-memory fakes. Run `make test-e2e-local` for changes affecting AI paths; it rebuilds the current Docker app image and runs the local AI smoke inside the container.

Cloudflare live tests, remote reindexing, live evals, and real Telegram operations are explicit external acceptance steps and are never part of the offline loop.

## Boundaries

`domain/` and `application/` contain no framework, Telegram, Cloudflare, or infrastructure imports. HTTP and Telegram adapters call explicit application services. SQL is the source of truth; vector projections are derived and repairable.

## Required checks before handoff

```bash
make format
make lint
make test
make test-integration
uv run python -m evals.run offline
```

The real Telegram two-group acceptance flow remains pending until a human runs it with test credentials. See [e2e-telegram.md](e2e-telegram.md).
