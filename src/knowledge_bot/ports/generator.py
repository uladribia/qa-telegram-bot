# SPDX-License-Identifier: MIT
"""Answer generation port (spec §17).

Model outputs are an external boundary, so the validated shapes are Pydantic
models living here next to the protocol that produces them.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One piece of evidence handed to the generator."""

    source_id: str
    text: str
    label: str
    authority: int


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A grounded generation request."""

    question: str
    evidence: list[EvidenceItem] = field(default_factory=list)


class GenerationOutput(BaseModel):
    """The validated generator output (spec §17)."""

    status: Literal["answered", "insufficient"]
    answer: str = ""
    source_ids: list[str] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    """An answer-quality verdict; ``error`` marks an unparseable judge call."""

    verdict: Literal["grounded", "unsupported", "wrong", "error"]
    reason: str = ""


@runtime_checkable
class Generator(Protocol):
    """Produces grounded answers from retrieved evidence."""

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate an answer, or report insufficient evidence."""
        ...

    async def judge(
        self, question: str, answer: str, evidence: list[str]
    ) -> JudgeVerdict:
        """Judge whether an answer is fully supported by its evidence."""
        ...
