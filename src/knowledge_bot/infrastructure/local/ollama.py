# SPDX-License-Identifier: MIT
"""Ollama REST adapters for the local runtime."""

import json
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.logging import log_content
from knowledge_bot.infrastructure.prompt import SYSTEM_PROMPT, render_user
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.generator import (
    GenerationOutput,
    GenerationRequest,
    parse_generation_output,
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _complete_output_schema() -> dict[str, object]:
    """Return the generation schema with every field marked required.

    ``GenerationOutput`` gives ``answer`` and ``source_ids`` defaults, so the
    plain schema leaves them optional and a small local model answers with
    ``{"status": "answered"}`` and nothing else — a reply that cannot be
    validated, on a request the service did answer. Marking the fields
    required only constrains decoding; it does not relax the contract, and the
    same parsed output is validated afterwards either way.
    """
    schema = json.loads(json.dumps(GenerationOutput.model_json_schema()))
    schema["required"] = sorted(schema.get("properties", {}))
    return schema


class _OllamaMessage(BaseModel):
    """Validated chat message returned by Ollama."""

    model_config = ConfigDict(extra="ignore")

    content: str


class _OllamaChatResponse(BaseModel):
    """Validated subset of an Ollama chat response."""

    model_config = ConfigDict(extra="ignore")

    message: _OllamaMessage


class _OllamaEmbedResponse(BaseModel):
    """Validated subset of an Ollama embedding response."""

    model_config = ConfigDict(extra="ignore")

    embeddings: list[list[float]] = Field(min_length=1)


class OllamaEmbedder(Embedder):
    """Embedding adapter for the local Ollama HTTP API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Configure one local embedding client."""
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api/embed"
        self._model = model
        self._timeout = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed one batch and validate count and dimensions."""
        log_content("ai_embedding_input", model=self._model, texts=texts)
        try:
            response = await self._client.post(
                self._url,
                json={"model": self._model, "input": texts},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = _OllamaEmbedResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            raise ModelUnavailableError("embedding") from error
        if len(payload.embeddings) != len(texts):
            raise ModelUnavailableError("embedding")
        dimensions = {len(vector) for vector in payload.embeddings}
        if len(dimensions) != 1 or 0 in dimensions:
            raise ModelUnavailableError("embedding")
        return [[float(value) for value in vector] for vector in payload.embeddings]


class OllamaGenerator:
    """Grounded generator using Ollama's structured chat endpoint."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Configure one local generation client."""
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._model = model
        self._timeout = timeout_seconds

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate one grounded answer from one model call.

        There is no retry for unreadable output. Ollama is already asked for a
        structured response, so a second call with "return valid JSON" hides
        the failure instead of attributing it, and it makes local behaviour
        differ from production.

        Args:
            request: The question and its evidence.

        Returns:
            The validated model output.

        Raises:
            ModelUnavailableError: Ollama failed or timed out.
            InvalidModelOutputError: The reply was empty, unparseable, or
                schema-invalid.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_user(request)},
        ]
        response = await self._complete(messages)
        return parse_generation_output(response.message.content)

    async def _complete(self, messages: list[dict[str, str]]) -> _OllamaChatResponse:
        """Call the Ollama chat endpoint once and validate its envelope.

        Args:
            messages: The system and user messages.

        Returns:
            The validated chat response.

        Raises:
            ModelUnavailableError: The call failed, timed out, or returned a
                response that is not a chat envelope.
        """
        log_content("ai_generation_prompt", model=self._model, messages=messages)
        try:
            response = await self._client.post(
                self._url,
                json={
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                    "format": _complete_output_schema(),
                    "options": {"temperature": 0},
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = _OllamaChatResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            raise ModelUnavailableError("generation") from error
        log_content(
            "ai_generation_response", model=self._model, content=payload.message.content
        )
        return payload
