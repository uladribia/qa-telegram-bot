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
)
from knowledge_bot.ports.system_one import SystemOneTransport


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
class MeteredSystemOneTransport:
    """A decision transport that meters an estimate of what it spends.

    A decision call costs tokens on the same Workers AI account as everything
    else, so it is metered like the others and recorded against the same budget.
    Without this the daily ledger would show free traffic that is not free.
    """

    inner: SystemOneTransport
    budget: AiBudget
    characters_per_neuron: float = 0.020

    async def decide(
        self,
        *,
        model: str,
        state: object,
        questions: dict[str, object],
    ) -> dict[str, object]:
        """Record an estimate, then ask the decision service.

        Args:
            model: The decision model to answer with.
            state: The state every question is asked about.
            questions: Typed questions keyed by decision name.

        Returns:
            The raw decision payload.
        """
        payload = await self.inner.decide(model=model, state=state, questions=questions)
        characters = len(str(state)) + len(str(questions))
        with contextlib.suppress(Exception):
            await self.budget.record_decision(characters, self.characters_per_neuron)
        return payload


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
