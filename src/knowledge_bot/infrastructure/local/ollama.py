# SPDX-License-Identifier: MIT
"""Ollama REST adapters for the local runtime."""

import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.generator import (
    GenerationOutput,
    GenerationRequest,
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


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


_SYSTEM_PROMPT = """You answer questions using ONLY the evidence below.

Rules:
1. Do not add facts not supported by evidence.
2. If evidence is insufficient or materially contradictory, return insufficient.
3. Prefer higher-authority evidence when sources conflict.
4. Never guess or extrapolate.
5. Return only JSON matching the schema.
6. source_ids must contain only supplied evidence ids.
7. Answer in the language of the user's question.

Return JSON:
{"status": "answered" | "insufficient", "answer": "...", "source_ids": ["..."]}"""


def _render_user(request: GenerationRequest) -> str:
    """Render grounded evidence for the local model."""
    evidence = "\n".join(
        f"[{item.source_id}] ({item.label}, authority={item.authority}) {item.text}"
        for item in request.evidence
    )
    return f"QUESTION:\n{request.question}\n\nEVIDENCE:\n{evidence or '(no evidence)'}"


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

    async def _attempt(self, messages: list[dict[str, str]]) -> GenerationOutput | None:
        """Run one request and parse its structured response."""
        try:
            response = await self._client.post(
                self._url,
                json={
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                    "format": GenerationOutput.model_json_schema(),
                    "options": {"temperature": 0},
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = _OllamaChatResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            raise ModelUnavailableError("generation") from error
        match = _JSON_OBJECT.search(payload.message.content)
        if match is None:
            return None
        try:
            return GenerationOutput.model_validate_json(match.group(0))
        except ValueError:
            return None

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate an answer, retrying only malformed model JSON once."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _render_user(request)},
        ]
        output = await self._attempt(messages)
        if output is None:
            output = await self._attempt(
                [
                    *messages,
                    {
                        "role": "user",
                        "content": "Return ONLY valid JSON matching the schema.",
                    },
                ]
            )
        return output or GenerationOutput(status="insufficient")
