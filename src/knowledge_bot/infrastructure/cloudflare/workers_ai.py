# SPDX-License-Identifier: MIT
"""Workers AI adapters for embeddings and grounded text generation."""

import re
from typing import Protocol, cast

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.generator import GenerationOutput, GenerationRequest
from knowledge_bot.ports.pairing import PairingModel, PairingOutput, PairMessage

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


def _field(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


class WorkersAIEmbedder:
    """Embedder backed by a Workers AI model."""

    def __init__(self, ai: AiRunner, model: str) -> None:
        """Create the embedder."""
        self._ai = ai
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Raises:
            ModelUnavailableError: When the embedding model call fails.
        """
        try:
            result = await self._ai.run(self._model, {"text": texts})
        except Exception as error:
            raise ModelUnavailableError("embedding") from error
        data = cast("list[list[float]]", _field(result, "data"))
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


class WorkersAIPairingModel(PairingModel):
    """Extract candidate question-answer pairs with Workers AI."""

    def __init__(self, ai: AiRunner, model: str) -> None:
        """Configure the pairing model."""
        self._ai = ai
        self._model = model

    async def pair(self, messages: list[PairMessage]) -> PairingOutput:
        """Return validated pairs for one bounded window."""
        prompt = (
            "Pair each answer-like message with its question in this chat window. "
            "Return only JSON matching the schema. Do not invent ids or pair "
            "unrelated messages.\n\n"
            + "\n".join(f"[{message.id}] {message.text}" for message in messages)
        )
        try:
            result = await self._ai.run(
                self._model,
                {
                    "messages": [{"role": "user", "content": prompt}],
                    "format": PairingOutput.model_json_schema(),
                },
            )
            content = _extract_content(result)
            match = _JSON_OBJECT.search(content)
            return (
                PairingOutput.model_validate_json(match.group(0))
                if match
                else PairingOutput()
            )
        except Exception as error:
            raise ModelUnavailableError("pairing") from error


class WorkersAIGenerator:
    """Grounded generator backed by a Workers AI chat model."""

    def __init__(self, ai: AiRunner, model: str) -> None:
        """Create the generator."""
        self._ai = ai
        self._model = model

    async def _run(self, messages: list[dict[str, str]]) -> object:
        """Run the chat model, raising a domain error on failure."""
        try:
            return await self._ai.run(self._model, {"messages": messages})
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
        """Generate a grounded answer, retrying once only on invalid JSON."""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _render_user(request)},
        ]
        output = await self._attempt(messages)
        if output is None:
            messages.append(
                {
                    "role": "user",
                    "content": "Return ONLY a valid JSON object matching the schema.",
                }
            )
            output = await self._attempt(messages)
        return output or GenerationOutput(status="insufficient")
