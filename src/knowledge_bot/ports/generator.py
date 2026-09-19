# SPDX-License-Identifier: MIT
"""Answer generation port (spec §17)."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


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


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """The validated generator output."""

    status: str
    answer: str = ""
    source_ids: list[str] = field(default_factory=list)


@runtime_checkable
class Generator(Protocol):
    """Produces grounded answers from retrieved evidence."""

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate an answer, or report insufficient evidence."""
        ...
