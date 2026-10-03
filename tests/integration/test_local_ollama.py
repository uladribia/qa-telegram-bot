# SPDX-License-Identifier: MIT
"""Offline contract tests for the local Ollama HTTP adapters."""

import json
from typing import cast

import httpx
import pytest

from knowledge_bot.domain.errors import InvalidModelOutputError
from knowledge_bot.infrastructure.local.ollama import OllamaEmbedder, OllamaGenerator
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_ollama_embedder_preserves_batch_order() -> None:
    """Send one batch request and return vectors in response order."""
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedder = OllamaEmbedder(client, "http://ollama:11434", "embeddinggemma")
        assert await embedder.embed(["first", "second"]) == [
            [1.0, 0.0],
            [0.0, 1.0],
        ]
    assert len(calls) == 1
    assert calls[0]["input"] == ["first", "second"]


@pytest.mark.asyncio
async def test_ollama_generator_raises_on_malformed_json_without_retrying() -> None:
    """Unreadable output is attributed, not retried into a silent abstention."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"message": {"content": "not json"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = OllamaGenerator(client, "http://ollama:11434", "gemma3:270m")
        with pytest.raises(InvalidModelOutputError) as raised:
            await generator.generate(GenerationRequest(question="unknown"))
    assert raised.value.code == "no_json"
    assert calls == 1


@pytest.mark.asyncio
async def test_ollama_generator_requires_every_output_field() -> None:
    """The local service is asked for a complete object, not a partial one.

    A small local model answers ``{"status": "answered"}`` when the fields
    with defaults are optional, which no answer can be built from. The
    constraint is on the request's schema, so the parsed output is still
    validated and a reply that fills the fields wrongly still fails.
    """
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "status": "answered",
                            "answer": "The local verification marker is VERIFIED-42.",
                            "source_ids": ["local-e2e"],
                        }
                    )
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = OllamaGenerator(client, "http://ollama:11434", "gemma3:270m")
        output = await generator.generate(
            GenerationRequest(
                question="What is the local verification marker?",
                evidence=[
                    EvidenceItem(
                        source_id="local-e2e",
                        text="The local verification marker is VERIFIED-42.",
                        label="Local E2E",
                        authority=50,
                        similarity=0.5,
                    )
                ],
            )
        )

    requested = calls[0]["format"]
    assert isinstance(requested, dict)
    required = cast(dict[str, object], requested)["required"]
    assert isinstance(required, list)
    assert set(cast(list[str], required)) == {"status", "answer", "source_ids"}
    assert output.source_ids == ["local-e2e"]
