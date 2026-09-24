# SPDX-License-Identifier: MIT
"""Fake embedder, vector store, and generator for offline tests."""

import math
from datetime import datetime

from knowledge_bot.application.classifier import LABELS, ClassifierHead
from knowledge_bot.domain.entities import SearchProjectionEntry
from knowledge_bot.domain.enums import ProjectionState
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.generator import GenerationOutput, GenerationRequest
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.lexical import LexicalMatch, LexicalRecord
from knowledge_bot.ports.review import ReviewItem
from knowledge_bot.ports.vector_store import VectorMatch, VectorRecord


class FakeEmbedder:
    """An embedder that returns the same fixed vector for every text."""

    def __init__(
        self,
        vector: list[float] | None = None,
        by_text: dict[str, list[float]] | None = None,
    ) -> None:
        """Create an embedder with a fixed output vector.

        Args:
            vector: The default vector, used for every text.
            by_text: Optional per-text vectors, for tests that need
                different texts to score differently.
        """
        self.vector = vector if vector is not None else [1.0, 0.0]
        self.by_text = by_text or {}
        self.calls: list[list[str]] = []
        self.fail = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return the fixed vector once per input text."""
        if self.fail:
            raise ModelUnavailableError("embedding")
        self.calls.append(texts)
        return [list(self.by_text.get(text, self.vector)) for text in texts]


def _matches(metadata: dict[str, object], filters: dict[str, object] | None) -> bool:
    if not filters:
        return True
    return all(metadata.get(key) == value for key, value in filters.items())


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


class FakeVectorStore:
    """An in-memory vector store with real cosine similarity and filtering."""

    def __init__(self) -> None:
        """Create an empty store."""
        self.records: dict[str, VectorRecord] = {}

    async def upsert(self, records: list[VectorRecord]) -> None:
        """Insert or replace vectors."""
        for record in records:
            self.records[record.id] = record

    async def query(
        self,
        vector: list[float],
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[VectorMatch]:
        """Return the closest matching vectors by cosine similarity."""
        candidates = [
            record
            for record in self.records.values()
            if _matches(record.metadata, filters)
        ]
        scored = sorted(
            ((_cosine(vector, record.values), record) for record in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [
            VectorMatch(id=record.id, score=score, metadata=dict(record.metadata))
            for score, record in scored[:top_k]
        ]

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors by id."""
        for vector_id in ids:
            self.records.pop(vector_id, None)


class FakeGenerator:
    """A generator that returns a fixed result and records its requests."""

    def __init__(self, result: GenerationOutput | None = None) -> None:
        """Create a generator with a fixed result."""
        self.result = (
            result if result is not None else GenerationOutput(status="insufficient")
        )
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Record the request and return the fixed result."""
        self.requests.append(request)
        return self.result


def linear_head(dimensions: int = 4) -> ClassifierHead:
    """Build a test head where the i-th unit vector maps to the i-th label.

    Coefficients are scaled by 2 so exact matches clear the confidence
    policy (top probability >= 0.60, margin >= 0.15) while any vector
    equidistant between two labels is ambiguous.
    """
    coef = []
    for index in range(len(LABELS)):
        row = [0.0] * dimensions
        if index < dimensions:
            row[index] = 2.0
        coef.append(tuple(row))
    return ClassifierHead(
        labels=LABELS, coef=tuple(coef), intercept=(0.0, 0.0, 0.0, 0.0)
    )


class FakeLexicalIndex:
    """An in-memory BM25 proxy: rank by matched-token count, then id."""

    def __init__(self) -> None:
        """Create an empty index."""
        self.records: dict[str, LexicalRecord] = {}

    async def upsert(self, records: list[LexicalRecord]) -> None:
        """Insert or replace lexical rows."""
        for record in records:
            self.records[record.id] = record

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[LexicalMatch]:
        """Return token-overlap matches, strongest first."""
        import re

        tokens = {token.casefold() for token in re.findall(r"\w+", query)}
        hits: list[tuple[int, str, LexicalRecord]] = []
        for record in self.records.values():
            if not _matches(record.metadata, filters):
                continue
            text_tokens = {
                token.casefold() for token in re.findall(r"\w+", record.text)
            }
            overlap = len(tokens & text_tokens)
            if overlap:
                hits.append((overlap, record.id, record))
        hits.sort(key=lambda item: (-item[0], item[1]))
        return [
            LexicalMatch(id=record.id, metadata=dict(record.metadata))
            for _, _, record in hits[:top_k]
        ]

    async def delete(self, ids: list[str]) -> None:
        """Delete lexical rows by id."""
        for vector_id in ids:
            self.records.pop(vector_id, None)


class InMemorySearchProjectionRepository:
    """In-memory projection manifest."""

    def __init__(self) -> None:
        """Create an empty manifest."""
        self.vector_ids: set[str] = set()
        self.updated_at: datetime | None = None
        self.entries: dict[str, SearchProjectionEntry] = {}

    async def reserve(
        self,
        vector_id: str,
        kind: str,
        object_id: str,
        version_id: str | None,
        updated_at: datetime,
    ) -> None:
        """Reserve a projection entry."""
        self.entries[vector_id] = SearchProjectionEntry(
            vector_id, kind, object_id, version_id, ProjectionState.PENDING, updated_at
        )

    async def mark_active(
        self, vector_id: str, version_id: str | None, updated_at: datetime
    ) -> None:
        """Mark a projection active."""
        entry = self.entries[vector_id]
        self.entries[vector_id] = SearchProjectionEntry(
            entry.vector_id,
            entry.kind,
            entry.object_id,
            version_id,
            ProjectionState.ACTIVE,
            updated_at,
        )
        self.vector_ids.add(vector_id)

    async def mark_failed(
        self,
        vector_id: str,
        version_id: str | None,
        error_code: str,
        updated_at: datetime,
    ) -> None:
        """Mark a projection failed."""
        entry = self.entries[vector_id]
        self.entries[vector_id] = SearchProjectionEntry(
            entry.vector_id,
            entry.kind,
            entry.object_id,
            version_id,
            ProjectionState.FAILED,
            updated_at,
            error_code,
        )

    async def get(self, vector_id: str) -> SearchProjectionEntry | None:
        """Return one projection entry."""
        return self.entries.get(vector_id)

    async def list_by_state(
        self, states: list[ProjectionState], limit: int
    ) -> list[SearchProjectionEntry]:
        """Return bounded repair entries."""
        return [item for item in self.entries.values() if item.state in states][:limit]

    async def list_vector_ids(self) -> list[str]:
        """Return every projected vector id."""
        return sorted(self.vector_ids)

    async def record(self, records: list[VectorRecord], updated_at: datetime) -> None:
        """Record successful vector upserts for old fixtures."""
        for record in records:
            version = record.metadata.get("version_id")
            version_id = str(version) if version is not None else None
            await self.reserve(
                record.id,
                str(record.metadata.get("kind", "unknown")),
                str(record.metadata.get("object_id", record.id)),
                version_id,
                updated_at,
            )
            await self.mark_active(record.id, version_id, updated_at)
        self.updated_at = updated_at

    async def delete(self, vector_ids: list[str]) -> None:
        """Remove vector ids from the manifest."""
        self.vector_ids.difference_update(vector_ids)
        for vector_id in vector_ids:
            self.entries.pop(vector_id, None)

    async def clear(self) -> None:
        """Clear the manifest."""
        self.vector_ids.clear()
        self.entries.clear()


class FakeSearchIndexSource:
    """A search-index source with fixed records."""

    def __init__(
        self,
        qa: list[IndexableQA] | None = None,
        messages: list[IndexableMessage] | None = None,
    ) -> None:
        """Create a source with the given records."""
        self.qa = qa or []
        self.messages = messages or []

    async def get_qa(self, version_id: str) -> IndexableQA | None:
        """Return one fixed Q&A record by version id."""
        return next((item for item in self.qa if item.version_id == version_id), None)

    async def get_current_qa_by_item_id(self, qa_item_id: str) -> IndexableQA | None:
        """Return the fixed current Q&A record for an item."""
        return next((item for item in self.qa if item.qa_item_id == qa_item_id), None)

    async def get_indexable_message(self, message_id: str) -> IndexableMessage | None:
        """Return one fixed message record."""
        return next(
            (item for item in self.messages if item.message_id == message_id), None
        )

    async def list_legacy_vector_ids(self) -> list[str]:
        """Return no legacy ids in the in-memory source."""
        return []

    async def list_qa(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableQA]:
        """Return the fixed Q&A records beyond the cursor."""
        qa = list(self.qa)
        if after is not None:
            qa = [item for item in qa if item.version_id > after]
        if limit is not None:
            qa = qa[:limit]
        return qa

    async def list_messages(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableMessage]:
        """Return the fixed message records beyond the cursor."""
        messages = list(self.messages)
        if after is not None:
            messages = [item for item in messages if item.message_id > after]
        if limit is not None:
            messages = messages[:limit]
        return messages


class FakeReviewSource:
    """A review source with fixed records."""

    def __init__(self, items: list[ReviewItem] | None = None) -> None:
        """Create a source with the given review items."""
        self.items = items or []

    async def list_current(self) -> list[ReviewItem]:
        """Return the fixed review items."""
        return list(self.items)
