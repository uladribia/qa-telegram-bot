# SPDX-License-Identifier: MIT
"""Fake embedder, vector store, and generator for offline tests."""

import math

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.generator import (
    GenerationOutput,
    GenerationRequest,
    JudgeVerdict,
)
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from knowledge_bot.ports.review import ReviewItem
from knowledge_bot.ports.vector_store import VectorMatch, VectorRecord


class FakeEmbedder:
    """An embedder that returns the same fixed vector for every text."""

    def __init__(self, vector: list[float] | None = None) -> None:
        """Create an embedder with a fixed output vector."""
        self.vector = vector if vector is not None else [1.0, 0.0]
        self.calls: list[list[str]] = []
        self.fail = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return the fixed vector once per input text."""
        if self.fail:
            raise ModelUnavailableError("embedding")
        self.calls.append(texts)
        return [list(self.vector) for _ in texts]


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
        self.verdict = JudgeVerdict(verdict="grounded")
        self.judgements: list[tuple[str, str, list[str]]] = []

    async def generate(self, request: GenerationRequest) -> GenerationOutput:
        """Record the request and return the fixed result."""
        self.requests.append(request)
        return self.result

    async def judge(
        self, question: str, answer: str, evidence: list[str]
    ) -> JudgeVerdict:
        """Record the judgement and return the fixed verdict."""
        self.judgements.append((question, answer, evidence))
        return self.verdict


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

    async def list_qa(self) -> list[IndexableQA]:
        """Return the fixed Q&A records."""
        return list(self.qa)

    async def list_messages(self) -> list[IndexableMessage]:
        """Return the fixed message records."""
        return list(self.messages)


class FakeReviewSource:
    """A review source with fixed records."""

    def __init__(self, items: list[ReviewItem] | None = None) -> None:
        """Create a source with the given review items."""
        self.items = items or []

    async def list_current(self) -> list[ReviewItem]:
        """Return the fixed review items."""
        return list(self.items)
