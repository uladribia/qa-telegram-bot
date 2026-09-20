# SPDX-License-Identifier: MIT
"""Metering decorators that record estimated Workers AI spend.

The adapters stay unaware of the budget: these wrappers surround the embedder
and the generator, estimate what each call cost, and record it. Metering must
never break a real answer, so any failure to record is swallowed.
"""

import contextlib
from dataclasses import dataclass

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.generator import (
    GenerationOutput,
    GenerationRequest,
    Generator,
    JudgeVerdict,
)


@dataclass(frozen=True, slots=True)
class MeteredEmbedder:
    """An embedder that meters its estimated neuron spend."""

    inner: Embedder
    budget: AiBudget

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed the texts, then record the estimate."""
        vectors = await self.inner.embed(texts)
        with contextlib.suppress(Exception):
            await self.budget.record_embedding(texts)
        return vectors


@dataclass(frozen=True, slots=True)
class MeteredGenerator:
    """A generator that meters its estimated neuron spend."""

    inner: Generator
    budget: AiBudget

    async def _record(self, prompt: str, response: str) -> None:
        with contextlib.suppress(Exception):
            await self.budget.record_chat(prompt, response)

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate an answer, then record the estimate."""
        result = await self.inner.generate(request)
        prompt = "\n".join(
            [request.question, *(item.text for item in request.evidence)]
        )
        await self._record(prompt, result.answer)
        return result

    async def judge(
        self, question: str, answer: str, evidence: list[str]
    ) -> JudgeVerdict:
        """Judge an answer, then record the estimate."""
        verdict = await self.inner.judge(question, answer, evidence)
        await self._record(
            f"{question}\n{answer}\n{''.join(evidence)}", verdict.verdict
        )
        return verdict
