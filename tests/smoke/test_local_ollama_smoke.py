# SPDX-License-Identifier: MIT
"""Explicit local Ollama smoke test."""

import os

import httpx
import pytest

from knowledge_bot.infrastructure.local.ollama import OllamaEmbedder
from knowledge_bot.infrastructure.settings import RuntimeMode, Settings

pytestmark = pytest.mark.e2e_local


@pytest.mark.asyncio
async def test_local_ollama_embedding_endpoint() -> None:
    """Call the configured local Ollama embedding endpoint once."""
    if os.getenv("RUN_LOCAL_AI_E2E") != "1":
        pytest.skip("set RUN_LOCAL_AI_E2E=1 to call local Ollama")
    settings = Settings(
        _env_file=".env.local",
        runtime=RuntimeMode.LOCAL,
        embedding_model="embeddinggemma",
        generation_model="gemma3:270m",
    )
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{settings.ollama_base_url.rstrip('/')}/api/tags"
            )
            response.raise_for_status()
        except httpx.HTTPError:
            pytest.skip("local Ollama is not running; run make dev-bootstrap first")
        embedder = OllamaEmbedder(
            client, settings.ollama_base_url, settings.embedding_model
        )
        vectors = await embedder.embed(["local runtime"])
    assert vectors and vectors[0]
