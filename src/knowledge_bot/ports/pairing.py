# SPDX-License-Identifier: MIT
"""Contracts for bounded question-answer pairing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class PairMessage:
    """One listener message supplied to a pairing model."""

    id: str
    text: str
    created_at: datetime
    reply_to_message_id: str | None = None


class PairCandidate(BaseModel):
    """One proposed question-answer pair."""

    question_id: str = Field(min_length=1)
    answer_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class PairingOutput(BaseModel):
    """Validated bounded output from a pairing model."""

    pairs: list[PairCandidate] = Field(default_factory=list, max_length=20)


@runtime_checkable
class PairingModel(Protocol):
    """Extract candidate question-answer pairs from a message window."""

    async def pair(self, messages: list[PairMessage]) -> PairingOutput:
        """Return validated candidate pairs for one bounded window."""
        ...
