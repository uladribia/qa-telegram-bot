# SPDX-License-Identifier: MIT
"""Ports for rebuilding and repairing the derived search index."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from knowledge_bot.domain.entities import SearchProjectionEntry
from knowledge_bot.domain.enums import ProjectionState
from knowledge_bot.domain.scope import GLOBAL_SCOPE


@dataclass(frozen=True, slots=True)
class IndexableQA:
    """An active Q&A version ready to be embedded."""

    qa_item_id: str
    version_id: str
    question: str
    answer: str
    authority: int
    canonical_key: str
    source_anchor: str | None = None
    url: str | None = None
    date: str | None = None
    author: str | None = None
    scope_key: str = GLOBAL_SCOPE


@dataclass(frozen=True, slots=True)
class IndexableMessage:
    """A message with text ready to be embedded."""

    message_id: str
    text: str
    source_kind: str
    authority: int
    conversation_id: str
    scope_key: str = GLOBAL_SCOPE
    author: str | None = None
    date: str | None = None
    question: str | None = None


@runtime_checkable
class SearchProjectionRepository(Protocol):
    """Durable state for derived vectors."""

    async def reserve(
        self,
        vector_id: str,
        kind: str,
        object_id: str,
        version_id: str | None,
        updated_at: datetime,
    ) -> None:
        """Reserve a stable vector id before projection."""
        ...

    async def mark_active(
        self, vector_id: str, version_id: str | None, updated_at: datetime
    ) -> None:
        """Mark a projection active."""
        ...

    async def mark_failed(
        self,
        vector_id: str,
        version_id: str | None,
        error_code: str,
        updated_at: datetime,
    ) -> None:
        """Mark a projection failed with a safe code."""
        ...

    async def get(self, vector_id: str) -> SearchProjectionEntry | None:
        """Return one projection entry."""
        ...

    async def list_by_state(
        self, states: list[ProjectionState], limit: int
    ) -> list[SearchProjectionEntry]:
        """Return a bounded repair batch."""
        ...

    async def list_vector_ids(self) -> list[str]:
        """Return every manifest vector id."""
        ...

    async def delete(self, vector_ids: list[str]) -> None:
        """Remove manifest entries."""
        ...

    async def clear(self) -> None:
        """Clear the manifest."""
        ...


class SearchIndexSource(Protocol):
    """Read indexable records from SQL truth."""

    async def get_qa(self, version_id: str) -> IndexableQA | None:
        """Return one current Q&A version."""
        ...

    async def get_current_qa_by_item_id(self, qa_item_id: str) -> IndexableQA | None:
        """Return the current active Q&A for an item."""
        ...

    async def get_indexable_message(self, message_id: str) -> IndexableMessage | None:
        """Return one eligible message."""
        ...

    async def list_qa(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableQA]:
        """Return a bounded batch of current Q&A records."""
        ...

    async def list_messages(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableMessage]:
        """Return a bounded batch of eligible messages."""
        ...

    async def list_legacy_vector_ids(self) -> list[str]:
        """Return legacy vector ids that a rebuild must remove."""
        ...
