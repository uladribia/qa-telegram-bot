# SPDX-License-Identifier: MIT
"""Ports for rebuilding the derived search index from D1."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class IndexableQA:
    """An active Q&A version ready to be embedded."""

    version_id: str
    question: str
    answer: str
    authority: int
    url: str | None = None
    date: str | None = None


@dataclass(frozen=True, slots=True)
class IndexableMessage:
    """A message with text, ready to be embedded."""

    message_id: str
    text: str
    source_type: str
    authority: int
    author: str | None = None
    date: str | None = None


@runtime_checkable
class SearchIndexSource(Protocol):
    """Reads the indexable records from the source of truth."""

    async def list_qa(self) -> list[IndexableQA]:
        """Return the active Q&A versions to index."""
        ...

    async def list_messages(self) -> list[IndexableMessage]:
        """Return the messages with text to index."""
        ...
