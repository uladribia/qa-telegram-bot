# SPDX-License-Identifier: MIT
"""Answer generation port (spec §17).

Model outputs are an external boundary, so the validated shapes are Pydantic
models living here next to the protocol that produces them.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator


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

    @model_validator(mode="after")
    def _validate_answer_and_sources(self) -> "GenerationOutput":
        """Require coherent answer text and unique source ids."""
        if len(self.source_ids) != len(set(self.source_ids)):
            message = "generation source ids must be unique"
            raise ValueError(message)
        if self.status == "answered" and (
            not self.answer.strip() or not self.source_ids
        ):
            message = "answered generation requires text and at least one source"
            raise ValueError(message)
        if self.status == "insufficient" and self.source_ids:
            message = "insufficient generation cannot cite sources"
            raise ValueError(message)
        return self


@runtime_checkable
class Generator(Protocol):
    """Produces grounded answers from retrieved evidence."""

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Generate an answer, or report insufficient evidence."""
        ...
