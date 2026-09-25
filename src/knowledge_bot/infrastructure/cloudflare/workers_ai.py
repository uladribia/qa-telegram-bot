# SPDX-License-Identifier: MIT
"""Workers AI adapters for embeddings and grounded text generation."""

import asyncio
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, cast

from loguru import logger

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.generator import GenerationOutput, GenerationRequest

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

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class AiRunner(Protocol):
    """The subset of the Workers AI binding used here."""

    async def run(self, model: str, inputs: dict[str, object]) -> object:
        """Run a model."""
        ...


class WorkersAIReranker:
    """Reranker backed by a Workers AI cross-encoder.

    One call scores the whole candidate list, so a rerank costs a single
    subrequest and a fraction of a neuron.
    """

    def __init__(self, ai: AiRunner, model: str, timeout_seconds: float = 5.0) -> None:
        """Create the reranker with a hard adapter deadline."""
        self._ai = ai
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def score(self, query: str, documents: list[str]) -> list[float]:
        """Score documents against the query in one batched call.

        Raises:
            ModelUnavailableError: When the reranker call fails or returns a
                score set that does not line up with the input.
        """
        if not documents:
            return []
        try:
            with _timed("reranking", self._model, sum(len(d) for d in documents)):
                result = await asyncio.wait_for(
                    self._ai.run(
                        self._model,
                        {
                            "query": query,
                            "contexts": [{"text": document} for document in documents],
                        },
                    ),
                    timeout=self._timeout_seconds,
                )
        except Exception as error:
            raise ModelUnavailableError("reranking") from error
        scored = _field(result, "response")
        if not isinstance(scored, list) or len(scored) != len(documents):
            raise ModelUnavailableError("reranking")
        by_id: dict[int, float] = {}
        for entry in scored:
            index = _as_int(_field(entry, "id"))
            value = _field(entry, "score")
            if index in by_id or not isinstance(value, (int, float)):
                raise ModelUnavailableError("reranking")
            by_id[index] = float(value)
        if len(by_id) != len(documents) or set(by_id) != set(range(len(documents))):
            raise ModelUnavailableError("reranking")
        return [by_id[index] for index in range(len(documents))]


def _as_int(value: object) -> int:
    """Return a reranker index as an int, or -1 when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return -1
    try:
        return int(value)
    except ValueError:
        return -1


def _field(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


@contextmanager
def _timed(operation: str, model: str, characters: int) -> Iterator[None]:
    """Log one Workers AI call's duration and size, never its payload.

    Args:
        operation: ``embedding`` or ``generation``.
        model: The Workers AI model identifier.
        characters: Request size in characters, a size proxy for the prompt.
    """
    started = time.perf_counter()
    try:
        yield
    except Exception as error:
        logger.bind(
            operation=operation,
            model=model,
            characters=characters,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error=type(error).__name__,
        ).warning("workers_ai_call_failed")
        raise
    logger.bind(
        operation=operation,
        model=model,
        characters=characters,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    ).info("workers_ai_call_completed")


class WorkersAIEmbedder:
    """Embedder backed by a Workers AI model."""

    def __init__(self, ai: AiRunner, model: str, timeout_seconds: float = 10.0) -> None:
        """Create the embedder with a hard adapter deadline."""
        self._ai = ai
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Raises:
            ModelUnavailableError: When the embedding model call fails.
        """
        try:
            with _timed("embedding", self._model, sum(len(text) for text in texts)):
                result = await asyncio.wait_for(
                    self._ai.run(self._model, {"text": texts}),
                    timeout=self._timeout_seconds,
                )
        except Exception as error:
            raise ModelUnavailableError("embedding") from error
        data = cast("list[list[float]]", _field(result, "data"))
        if len(data) != len(texts) or any(not row for row in data):
            raise ModelUnavailableError("embedding")
        dimensions = {len(row) for row in data}
        if len(dimensions) != 1:
            raise ModelUnavailableError("embedding")
        return [[float(value) for value in row] for row in data]


def _extract_content(result: object) -> str:
    choices = _field(result, "choices")
    if isinstance(choices, list) and choices:
        message = _field(choices[0], "message")
        content = _field(message, "content")
        if isinstance(content, str):
            return content
    response = _field(result, "response")
    return response if isinstance(response, str) else ""


def _render_user(request: GenerationRequest) -> str:
    evidence_lines = [
        f"[{item.source_id}] ({item.label}, authority={item.authority}) {item.text}"
        for item in request.evidence
    ]
    evidence = "\n".join(evidence_lines) if evidence_lines else "(no evidence)"
    return f"QUESTION:\n{request.question}\n\nEVIDENCE:\n{evidence}"


class WorkersAIGenerator:
    """Grounded generator backed by a Workers AI chat model."""

    def __init__(
        self,
        ai: AiRunner,
        model: str,
        timeout_seconds: float = 35.0,
        max_tokens: int = 1024,
    ) -> None:
        """Create the generator with a hard adapter deadline.

        Args:
            ai: The Workers AI binding.
            model: The chat model to run.
            timeout_seconds: Hard deadline for one call.
            max_tokens: Output budget for one call. The model is a reasoning
                model and spends most of its output thinking; on evidence that
                does not contain the answer it can deliberate past 2600 tokens
                and 50 seconds. The cap turns that into a truncated response
                that becomes ``insufficient`` instead of a timeout.
        """
        self._ai = ai
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens

    async def _run(self, messages: list[dict[str, str]]) -> object:
        """Run the chat model, raising a domain error on failure.

        The request deliberately carries no ``response_format``. The system
        prompt already fixes the JSON shape and the output is parsed and
        validated locally, so the structured-output mode only added a Workers
        AI latency path that could hang past the deadline.
        """
        try:
            with _timed(
                "generation",
                self._model,
                sum(len(message["content"]) for message in messages),
            ):
                return await asyncio.wait_for(
                    self._ai.run(
                        self._model,
                        {"messages": messages, "max_tokens": self._max_tokens},
                    ),
                    timeout=self._timeout_seconds,
                )
        except Exception as error:
            raise ModelUnavailableError("generation") from error

    async def _attempt(self, messages: list[dict[str, str]]) -> GenerationOutput | None:
        """Run the model once and parse its JSON output, or return ``None``."""
        result = await self._run(messages)
        content = _extract_content(result)
        match = _JSON_OBJECT.search(content)
        if match is None:
            return None
        try:
            return GenerationOutput.model_validate_json(match.group(0))
        except ValueError:
            return None

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate one grounded answer; malformed output becomes insufficient."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _render_user(request)},
        ]
        output = await self._attempt(messages)
        return output or GenerationOutput(status="insufficient")
