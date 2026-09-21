# SPDX-License-Identifier: MIT
"""Tests for the retrieval service (fakes with real cosine similarity)."""

from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeVectorStore


async def test_retrieval_filters_status_and_ranks_by_similarity() -> None:
    """Only active Q&A is retrieved, and results are ranked by similarity."""
    store = FakeVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="qa1",
                values=[1.0, 0.0],
                metadata={
                    "kind": "qa_version",
                    "status": "active",
                    "text": "resposta",
                    "authority": 90,
                    "question": "Quan?",
                },
            ),
            VectorRecord(
                id="qa-draft",
                values=[1.0, 0.0],
                metadata={
                    "kind": "qa_version",
                    "status": "under_review",
                    "text": "esborrany",
                    "authority": 30,
                },
            ),
            VectorRecord(
                id="m1",
                values=[1.0, 0.0],
                metadata={"kind": "message", "text": "important", "authority": 40},
            ),
            VectorRecord(
                id="m2",
                values=[0.0, 1.0],
                metadata={"kind": "message", "text": "irrelevant", "authority": 40},
            ),
        ]
    )
    service = RetrievalService(
        embedder=FakeEmbedder([1.0, 0.0]),
        vectors=store,
        qa_top_k=5,
        message_top_k=5,
    )
    retrieved = await service.retrieve("pregunta")
    assert [item.source_id for item in retrieved.qa] == ["qa1"]
    assert retrieved.qa[0].similarity == 1.0
    assert retrieved.qa[0].question == "Quan?"
    assert [item.source_id for item in retrieved.messages] == ["m1", "m2"]
    assert retrieved.messages[0].label == "Grup"


async def test_scoped_retrieval_sees_global_and_own_group_only() -> None:
    """A question in group A sees global knowledge and group A only."""
    store = FakeVectorStore()
    vector = [1.0, 0.0]
    await store.upsert(
        [
            VectorRecord(
                id="qa-global",
                values=vector,
                metadata={"kind": "qa_version", "status": "active", "scope": "global"},
            ),
            VectorRecord(
                id="qa-a",
                values=vector,
                metadata={"kind": "qa_version", "status": "active", "scope": "-100"},
            ),
            VectorRecord(
                id="qa-b",
                values=vector,
                metadata={"kind": "qa_version", "status": "active", "scope": "-200"},
            ),
            VectorRecord(
                id="msg-a",
                values=vector,
                metadata={"kind": "message", "scope": "-100"},
            ),
            VectorRecord(
                id="msg-b",
                values=vector,
                metadata={"kind": "message", "scope": "-200"},
            ),
        ]
    )
    service = RetrievalService(
        embedder=FakeEmbedder(vector),
        vectors=store,
        qa_top_k=5,
        message_top_k=5,
    )
    retrieved = await service.retrieve("pregunta", conversation_id="-100")
    qa_ids = [item.source_id for item in retrieved.qa]
    message_ids = [item.source_id for item in retrieved.messages]
    assert "qa-global" in qa_ids and "qa-a" in qa_ids
    assert "qa-b" not in qa_ids
    assert "msg-a" in message_ids
    assert "msg-b" not in message_ids


async def test_group_variant_beats_the_global_answer() -> None:
    """When both scopes match equally, the group variant comes first."""
    store = FakeVectorStore()
    vector = [1.0, 0.0]
    await store.upsert(
        [
            VectorRecord(
                id="qa-global",
                values=vector,
                metadata={
                    "kind": "qa_version",
                    "status": "active",
                    "scope": "global",
                    "anchor": "same-question",
                },
            ),
            VectorRecord(
                id="qa-group",
                values=vector,
                metadata={
                    "kind": "qa_version",
                    "status": "active",
                    "scope": "-100",
                    "anchor": "same-question",
                },
            ),
        ]
    )
    service = RetrievalService(embedder=FakeEmbedder(vector), vectors=store)
    retrieved = await service.retrieve("pregunta", conversation_id="-100")
    # The same canonical question: the group variant replaces the global one.
    assert [item.source_id for item in retrieved.qa] == ["qa-group"]


async def test_group_variant_suppresses_a_better_scoring_global_match() -> None:
    """The group variant always wins its question, even with lower similarity."""
    store = FakeVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="qa-global",
                values=[1.0, 0.0],
                metadata={
                    "kind": "qa_version",
                    "status": "active",
                    "scope": "global",
                    "anchor": "same-question",
                },
            ),
            VectorRecord(
                id="qa-group",
                values=[0.6, 0.8],  # cosine ~0.76 vs 1.0 for the global one
                metadata={
                    "kind": "qa_version",
                    "status": "active",
                    "scope": "-100",
                    "anchor": "same-question",
                },
            ),
        ]
    )
    service = RetrievalService(embedder=FakeEmbedder([1.0, 0.0]), vectors=store)
    retrieved = await service.retrieve("pregunta", conversation_id="-100")
    assert [item.source_id for item in retrieved.qa] == ["qa-group"]
