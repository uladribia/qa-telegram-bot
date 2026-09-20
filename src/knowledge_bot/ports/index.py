# SPDX-License-Identifier: MIT
"""Ports for rebuilding the derived search index from D1."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from knowledge_bot.domain.scope import GLOBAL_SCOPE


@dataclass(frozen=True, slots=True)
class IndexableQA:
    """An active Q&A version ready to be embedded.

    ``url`` and ``author`` are mutually exclusive citations: a web snapshot
    version carries the anchored URL, a human correction carries its author.
    """

    version_id: str
    question: str
    answer: str
    authority: int
    anchor: str | None = None
    url: str | None = None
    date: str | None = None
    author: str | None = None
    scope: str = GLOBAL_SCOPE


@dataclass(frozen=True, slots=True)
class IndexableMessage:
    """A message with text, ready to be embedded."""

    message_id: str
    text: str
    source_type: str
    authority: int
    conversation_id: str
    author: str | None = None
    date: str | None = None


@runtime_checkable
class SearchIndexSource(Protocol):
    """Reads the indexable records from the source of truth."""

    async def get_qa(self, version_id: str) -> IndexableQA | None:
        """Return one Q&A version to index, by id."""
        ...

    async def list_qa(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableQA]:
        """Return the active Q&A versions to index.

        Args:
            after: Return only versions with id greater than this (cursor).
            limit: Maximum number of versions to return.

        Returns:
            The next batch of versions, ordered by id.
        """
        ...

    async def list_messages(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableMessage]:
        """Return the messages with text to index.

        Args:
            after: Return only messages with id greater than this (cursor).
            limit: Maximum number of messages to return.

        Returns:
            The next batch of messages, ordered by id.
        """
        ...
