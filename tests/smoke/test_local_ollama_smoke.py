# SPDX-License-Identifier: MIT
"""Explicit local Ollama smoke test."""

import os
from datetime import UTC, datetime

import httpx
import pytest

from knowledge_bot.infrastructure.local.ollama import (
    OllamaEmbedder,
    OllamaGenerator,
    OllamaPairingModel,
)
from knowledge_bot.infrastructure.settings import RuntimeMode, Settings
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest
from knowledge_bot.ports.pairing import PairMessage

pytestmark = pytest.mark.e2e_local
NOW = datetime(2026, 9, 24, tzinfo=UTC)


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

        generator = OllamaGenerator(
            client, settings.ollama_base_url, settings.generation_model
        )
        generated = await generator.generate(
            GenerationRequest(
                question="What is the local verification marker?",
                evidence=[
                    EvidenceItem(
                        source_id="local-e2e",
                        text="The local verification marker is VERIFIED-LOCAL-42.",
                        label="Local E2E",
                        authority=50,
                    )
                ],
            )
        )
        assert generated.status in {"answered", "insufficient"}
        if generated.status == "answered":
            assert generated.source_ids == ["local-e2e"]

        pairing = OllamaPairingModel(
            client, settings.ollama_base_url, settings.generation_model
        )
        paired = await pairing.pair(
            [
                PairMessage(id="q1", text="Quan entrenem?", created_at=NOW),
                PairMessage(id="a1", text="A les sis.", created_at=NOW),
            ]
        )
        assert all(
            pair.question_id in {"q1", "a1"} and pair.answer_id in {"q1", "a1"}
            for pair in paired.pairs
        )
