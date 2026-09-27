# SPDX-License-Identifier: MIT
"""Answer generation port (spec §17).

Model outputs are an external boundary, so the validated shapes are Pydantic
models living here next to the protocol that produces them. Parsing lives here
too, so every adapter reports the same failure the same way: output that cannot
be read raises ``InvalidModelOutputError`` and never becomes an abstention.
"""

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from knowledge_bot.domain.errors import InvalidModelOutputError

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One piece of evidence handed to the generator."""

    source_id: str
    text: str
    label: str
    authority: int
    similarity: float
    provenance: str = "official"
    source_kind: str | None = None
    #: The question this item answers. Retrieval matches on it, so the model
    #: needs it to tell a near-miss from an answer. For curated Q&A it is the
    #: canonical question; for reported evidence it is the question the message
    #: replied to.
    question: str | None = None


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
        """Generate an answer, or report insufficient evidence.

        Raises:
            InvalidModelOutputError: The model replied with output that
                cannot be parsed or validated. This is a provider failure, not
                the model declining to answer.
            ModelUnavailableError: The provider failed or timed out.
        """
        ...


def parse_generation_output(content: str) -> GenerationOutput:
    """Parse one model reply into validated output.

    Args:
        content: The raw text the model returned.

    Returns:
        The validated output.

    Raises:
        InvalidModelOutputError: The reply is empty, holds no JSON object, or
            does not validate. The raw text is never carried in the error.
    """
    if not content.strip():
        raise InvalidModelOutputError("missing_content")
    match = _JSON_OBJECT.search(content)
    if match is None:
        raise InvalidModelOutputError("no_json")
    try:
        return GenerationOutput.model_validate_json(match.group(0))
    except ValueError as error:
        raise InvalidModelOutputError("schema_validation") from error
