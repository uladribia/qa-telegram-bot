# SPDX-License-Identifier: MIT
"""Tests for the retrieval service (fakes with real cosine similarity)."""

from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeVectorStore

SPACE_A = "sp_" + "1" * 32
SPACE_B = "sp_" + "2" * 32
SCOPE_A = scope_for_space(SPACE_A)
SCOPE_B = scope_for_space(SPACE_B)


async def test_retrieval_filters_status_and_ranks_by_similarity() -> None:
    """Only active Q&A is retrieved, and results are ranked by similarity."""
    store = FakeVectorStore()
    await store.upsert(
        [
            VectorRecord(
                id="qa1",
                values=[1.0, 0.0],
                metadata={
                    "kind": "qa",
                    "status": "active",
                    "scope_key": GLOBAL_SCOPE,
                    "object_id": "qa1",
                    "version_id": "qav1",
                    "canonical_key": "same-question",
                    "text": "resposta",
                    "authority": 90,
                    "question": "Quan?",
                },
            ),
            VectorRecord(
                id="qa-draft",
                values=[1.0, 0.0],
                metadata={
                    "kind": "qa",
                    "status": "under_review",
                    "scope_key": GLOBAL_SCOPE,
                    "text": "esborrany",
                    "authority": 30,
                },
            ),
            VectorRecord(
                id="m1",
                values=[1.0, 0.0],
                metadata={
                    "kind": "message_evidence",
                    "scope_key": GLOBAL_SCOPE,
                    "text": "important",
                    "authority": 40,
                },
            ),
            VectorRecord(
                id="m2",
                values=[0.0, 1.0],
                metadata={
                    "kind": "message_evidence",
                    "scope_key": GLOBAL_SCOPE,
                    "text": "irrelevant",
                    "authority": 40,
                },
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
                metadata={
                    "kind": "qa",
                    "status": "active",
                    "scope_key": GLOBAL_SCOPE,
                },
            ),
            VectorRecord(
                id="qa-a",
                values=vector,
                metadata={"kind": "qa", "status": "active", "scope_key": SCOPE_A},
            ),
            VectorRecord(
                id="qa-b",
                values=vector,
                metadata={"kind": "qa", "status": "active", "scope_key": SCOPE_B},
            ),
            VectorRecord(
                id="msg-a",
                values=vector,
                metadata={"kind": "message_evidence", "scope_key": SCOPE_A},
            ),
            VectorRecord(
                id="msg-b",
                values=vector,
                metadata={"kind": "message_evidence", "scope_key": SCOPE_B},
            ),
        ]
    )
    service = RetrievalService(
        embedder=FakeEmbedder(vector),
        vectors=store,
        qa_top_k=5,
        message_top_k=5,
    )
    retrieved = await service.retrieve("pregunta", SPACE_A)
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
                    "kind": "qa",
                    "status": "active",
                    "scope_key": GLOBAL_SCOPE,
                    "canonical_key": "same-question",
                },
            ),
            VectorRecord(
                id="qa-group",
                values=vector,
                metadata={
                    "kind": "qa",
                    "status": "active",
                    "scope_key": SCOPE_A,
                    "canonical_key": "same-question",
                },
            ),
        ]
    )
    service = RetrievalService(embedder=FakeEmbedder(vector), vectors=store)
    retrieved = await service.retrieve("pregunta", SPACE_A)
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
                    "kind": "qa",
                    "status": "active",
                    "scope_key": GLOBAL_SCOPE,
                    "canonical_key": "same-question",
                },
            ),
            VectorRecord(
                id="qa-group",
                values=[0.6, 0.8],  # cosine ~0.76 vs 1.0 for the global one
                metadata={
                    "kind": "qa",
                    "status": "active",
                    "scope_key": SCOPE_A,
                    "canonical_key": "same-question",
                },
            ),
        ]
    )
    service = RetrievalService(embedder=FakeEmbedder([1.0, 0.0]), vectors=store)
    retrieved = await service.retrieve("pregunta", SPACE_A)
    assert [item.source_id for item in retrieved.qa] == ["qa-group"]
