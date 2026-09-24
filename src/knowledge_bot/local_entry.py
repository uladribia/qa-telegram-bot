# SPDX-License-Identifier: MIT
"""Uvicorn entrypoint for the canonical local runtime."""

import asyncio

from fastapi import Request

from knowledge_bot.adapters.http.app import create_app
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.infrastructure.local.composition import build_context
from knowledge_bot.infrastructure.settings import Settings

_cached_context: AppContext | None = None
_database = None
_http_client = None
_lock = asyncio.Lock()


async def _resolve_context(_request: Request) -> AppContext:
    """Build and cache the local graph on the first request."""
    global _cached_context, _database, _http_client
    if _cached_context is None:
        async with _lock:
            if _cached_context is None:
                context, database, client = await build_context(Settings())
                _cached_context = context
                _database = database
                _http_client = client
    return _cached_context


app = create_app(_resolve_context)


@app.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    """Report local database and Ollama readiness without inference."""
    context = await _resolve_context(request)
    if _database is None:
        return {"status": "not-ready"}
    cursor = await _database.connection.execute("SELECT 1")
    await cursor.fetchone()
    cursor = await _database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'local_vectors'"
    )
    if await cursor.fetchone() is None:
        return {"status": "not-ready"}
    if _http_client is None:
        return {"status": "not-ready"}
    try:
        response = await _http_client.get(
            f"{context.settings.ollama_base_url.rstrip('/')}/api/tags"
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = {
            str(model.get("name", "")) for model in models if isinstance(model, dict)
        }
        required = {context.settings.embedding_model, context.settings.generation_model}
        available = {
            required_name
            for required_name in required
            if required_name in names or f"{required_name}:latest" in names
        }
        if available != required:
            return {"status": "not-ready"}
    except Exception:
        return {"status": "not-ready"}
    return {"status": "ok"}
