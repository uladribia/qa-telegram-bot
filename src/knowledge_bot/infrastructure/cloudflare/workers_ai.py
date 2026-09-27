# SPDX-License-Identifier: MIT
"""Workers AI adapters for embeddings and grounded text generation."""

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, cast

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.logging import log_content
from knowledge_bot.infrastructure.prompt import SYSTEM_PROMPT, render_user
from knowledge_bot.ports.generator import (
    GenerationOutput,
    GenerationRequest,
    parse_generation_output,
)


class AiRunner(Protocol):
    """The subset of the Workers AI binding used here."""

    async def run(self, model: str, inputs: dict[str, object]) -> object:
        """Run a model."""
        ...


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
    fields = {
        "operation": operation,
        "model": model,
        "characters": characters,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    try:
        yield
    except Exception as error:
        logging.getLogger("knowledge_bot.ai").warning(
            "workers_ai_call_failed", extra={**fields, "error": type(error).__name__}
        )
        raise
    logging.getLogger("knowledge_bot.ai").info(
        "workers_ai_call_completed", extra=fields
    )


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
            log_content("ai_embedding_input", model=self._model, texts=texts)
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
            log_content("ai_generation_prompt", model=self._model, messages=messages)
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

    async def _reply(self, messages: list[dict[str, str]]) -> str:
        """Run the model once and return its raw reply text.

        Args:
            messages: The system and user messages.

        Returns:
            The model's reply content, empty when the reply carried none.
        """
        content = _extract_content(await self._run(messages))
        log_content("ai_generation_response", model=self._model, content=content)
        return content

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate one grounded answer from one model call.

        Args:
            request: The question and its evidence.

        Returns:
            The validated model output.

        Raises:
            ModelUnavailableError: The provider failed or timed out.
            InvalidModelOutputError: The reply was empty, unparseable, or
                schema-invalid. This is never reported as an abstention.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_user(request)},
        ]
        return parse_generation_output(await self._reply(messages))
