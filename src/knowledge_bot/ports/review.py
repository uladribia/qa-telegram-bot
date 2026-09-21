# SPDX-License-Identifier: MIT
"""Ports for building the human knowledge review report."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """The current answer of one Q&A item, for review reporting.

    ``superseded_origin`` is the origin of the version this one replaced, if
    any: it is how the report spots a renewal that overwrote a correction.
    """

    canonical_key: str
    question: str
    scope: str
    answer: str
    origin: str
    created_at: datetime
    status: str
    superseded_origin: str | None = None


@runtime_checkable
class ReviewSource(Protocol):
    """Reads the current Q&A state from the source of truth."""

    async def list_current(self) -> list[ReviewItem]:
        """Return the current version of every active Q&A item."""
        ...
