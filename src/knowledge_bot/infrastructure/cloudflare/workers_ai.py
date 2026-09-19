# SPDX-License-Identifier: MIT
"""Workers AI adapters: embeddings and grounded text generation.

``env.AI`` is wrapped by the Workers runtime, so ``run`` returns Python data.
The adapter only depends on a small protocol so it is testable with fakes.
"""

import json
import re
from typing import Protocol, cast

from knowledge_bot.contracts.ai import GenerationOutput, JudgeOutput
from knowledge_bot.ports.generator import (
    GenerationRequest,
    GenerationResult,
    JudgeVerdict,
)

_SYSTEM_PROMPT = """You answer questions using ONLY the evidence below.

Rules:
1. Do not add facts not supported by evidence.
2. If evidence conflicts materially, return status "insufficient".
3. If the answer is unknown, return status "insufficient".
4. Prefer higher-authority evidence.
5. A source marked "in_review" cannot alone establish a fact.
6. Return only JSON matching the schema.
7. source_ids must only contain IDs from the supplied evidence.
8. Answer in the language of the user's question.

Return JSON:
{"status": "answered" | "insufficient", "answer": "...", "source_ids": ["..."]}"""

_JUDGE_SYSTEM_PROMPT = """You audit an assistant's answer against its evidence.

Rules:
1. "grounded": every factual claim in the answer is supported by the evidence,
   and the answer addresses the question.
2. "unsupported": the answer contains a factual claim absent from the evidence.
3. "wrong": the answer contradicts the evidence.
4. Judge only what the answer claims, not the evidence's quality.
5. Return only JSON matching the schema.

Return JSON:
{"verdict": "grounded" | "unsupported" | "wrong", "reason": "..."}"""

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
        """Create the embedder.

        Args:
            ai: The Workers AI binding.
            model: The embedding model name.
        """
        self._ai = ai
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        result = await self._ai.run(self._model, {"text": texts})
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


def _parse_output(content: str) -> GenerationOutput | None:
    match = _JSON_OBJECT.search(content)
    if match is None:
        return None
    try:
        return GenerationOutput.model_validate_json(match.group(0))
    except ValueError:
        return None


def _render_user(request: GenerationRequest) -> str:
    evidence_lines = [
        f"[{item.source_id}] ({item.label}, authority={item.authority}) {item.text}"
        for item in request.evidence
    ]
    evidence = "\n".join(evidence_lines) if evidence_lines else "(no evidence)"
    return f"QUESTION:\n{request.question}\n\nEVIDENCE:\n{evidence}"


class WorkersAIGenerator:
    """Grounded generator backed by a Workers AI chat model."""

    def __init__(self, ai: AiRunner, model: str) -> None:
        """Create the generator.

        Args:
            ai: The Workers AI binding.
            model: The generation model name.
        """
        self._ai = ai
        self._model = model

    async def _attempt(self, messages: list[dict[str, str]]) -> GenerationOutput | None:
        result = await self._ai.run(self._model, {"messages": messages})
        return _parse_output(_extract_content(result))

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate a grounded answer, retrying once on invalid JSON."""
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
        if output is None:
            return GenerationResult(status="insufficient")
        return GenerationResult(
            status=output.status,
            answer=output.answer,
            source_ids=output.source_ids,
        )

    async def judge(
        self, question: str, answer: str, evidence: list[str]
    ) -> JudgeVerdict:
        """Judge whether an answer is fully supported by its evidence."""
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _render_judge(question, answer, evidence)},
        ]
        result = await self._ai.run(self._model, {"messages": messages})
        output = _parse_judge(_extract_content(result))
        if output is None:
            return JudgeVerdict(verdict="error", reason="unparseable judge output")
        return JudgeVerdict(verdict=output.verdict, reason=output.reason)


def _parse_judge(content: str) -> JudgeOutput | None:
    match = _JSON_OBJECT.search(content)
    if match is None:
        return None
    try:
        return JudgeOutput.model_validate_json(match.group(0))
    except ValueError:
        return None


def _render_judge(question: str, answer: str, evidence: list[str]) -> str:
    evidence_text = "\n".join(evidence) if evidence else "(no evidence)"
    return f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\nEVIDENCE:\n{evidence_text}"


def dumps(value: object) -> str:
    """Serialise a value to compact JSON (helper for callers)."""
    return json.dumps(value)
