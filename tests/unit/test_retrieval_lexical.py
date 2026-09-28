# SPDX-License-Identifier: MIT
"""Unit tests for the lexical leg's tokenization, gates, and merge."""

import pytest

from knowledge_bot.application.retrieval import (
    GLOBAL_SCOPE,
    RetrievalService,
    RetrievedEvidence,
    _merge_lexical,
)
from knowledge_bot.infrastructure.sql.lexical import lexical_match_query
from knowledge_bot.ports.index import LexicalMatch
from knowledge_bot.ports.vector_store import VectorMatch, VectorRecord
from tests.fakes.ai import FakeEmbedder, FakeLexicalIndex, FakeVectorStore

QUESTION = "Quan entrenen els equips?"


def _record(vector_id: str, values: list[float], authority: int = 90) -> VectorRecord:
    """Build a projected Q&A vector record."""
    return VectorRecord(
        id=vector_id,
        values=values,
        metadata={
            "kind": "qa",
            "status": "active",
            "scope_key": GLOBAL_SCOPE,
            "object_id": vector_id.split(":")[-1],
            "canonical_key": vector_id,
            "authority": authority,
            "question": "Com es paguen?",
            "text": "text",
        },
    )


async def _store(*records: VectorRecord) -> FakeVectorStore:
    """Build a vector store holding the given records."""
    store = FakeVectorStore()
    await store.upsert(list(records))
    return store


def _lexical(*scores: tuple[str, float]) -> FakeLexicalIndex:
    """Build a lexical index returning the given id/score pairs."""
    return FakeLexicalIndex(
        [
            LexicalMatch(id=vector_id, score=score, metadata={"authority": 90})
            for vector_id, score in scores
        ]
    )


async def _retrieve(
    store: FakeVectorStore,
    lexical: FakeLexicalIndex | None,
) -> RetrievedEvidence:
    """Run retrieval with the leg configured the way production configures it."""
    service = RetrievalService(
        embedder=FakeEmbedder(),
        vectors=store,
        lexical=lexical,
        qa_top_k=5,
        message_top_k=2,
        floor=0.35,
        qa_answer_top_k=3,
        qa_answer_min_strength=0.5,
        qa_answer_relative_cut=0.5,
        lexical_authority=45,
    )
    return await service.retrieve(QUESTION)


def test_content_tokens_are_ored_not_anded() -> None:
    """An AND of every token is what killed the first lexical leg."""
    query = lexical_match_query("com es fan els pagaments?")
    assert query == '"fan" OR "pagaments"'
    assert " OR " in query
    assert "com" not in query
    assert "els" not in query


def test_a_question_of_only_stopwords_yields_no_query() -> None:
    """Nothing to match means the leg contributes nothing, not everything."""
    assert lexical_match_query("de la el que i a en") == ""
    assert lexical_match_query("") == ""


def test_tokens_are_quoted_so_fts_cannot_read_user_syntax() -> None:
    """A user word must never be interpreted as an FTS5 operator."""
    assert lexical_match_query('cluber OR NEAR("x")') == '"cluber" OR "near"'


def test_query_tokens_are_folded_like_the_index() -> None:
    """The table strips diacritics, so an unfolded query would never match."""
    assert "llicencia" in lexical_match_query("Com es paguen la llicència?")
    assert "llicència" not in lexical_match_query("Com es paguen la llicència?")


@pytest.mark.asyncio
async def test_gate_zero_keeps_the_leg_subordinate_to_the_floor() -> None:
    """With nothing above the floor, the leg does not run at all."""
    lexical = _lexical(("qa:x", 9.0))
    store = await _store(_record("qa:weak", [0.0, 1.0]))

    retrieved = await _retrieve(store, lexical)

    assert retrieved.lexical_qa == []
    assert lexical.queries == []


@pytest.mark.asyncio
async def test_gate_one_drops_matches_with_no_discriminative_weight() -> None:
    """A near-zero strength means the query matched only ubiquitous words."""
    store = await _store(_record("qa:ok", [1.0, 0.0]))

    retrieved = await _retrieve(store, _lexical(("qa:weak", 0.01)))

    assert retrieved.lexical_qa == []


@pytest.mark.asyncio
async def test_gate_two_drops_the_weak_tail() -> None:
    """Only hits near this query's own best survive the relative cut."""
    store = await _store(_record("qa:ok", [1.0, 0.0]))
    lexical = _lexical(("qa:a", 4.0), ("qa:b", 2.2), ("qa:c", 0.6))

    retrieved = await _retrieve(store, lexical)

    assert [item.source_id for item in retrieved.lexical_qa] == ["qa:a", "qa:b"]


@pytest.mark.asyncio
async def test_gate_three_caps_the_leg_at_its_own_top_k() -> None:
    """The leg contributes at most its own width, not the semantic width."""
    store = await _store(_record("qa:ok", [1.0, 0.0]))
    lexical = _lexical(*[(f"qa:{index}", 10.0 - index / 10) for index in range(10)])

    retrieved = await _retrieve(store, lexical)

    assert len(retrieved.lexical_qa) == 3


@pytest.mark.asyncio
async def test_a_lexical_hit_keeps_official_provenance_at_lower_authority() -> None:
    """The text is the club's own; only the match is unverified."""
    store = await _store(_record("qa:ok", [1.0, 0.0]))

    retrieved = await _retrieve(store, _lexical(("qa:new", 5.0)))

    hit = next(item for item in retrieved.lexical_qa if item.source_id == "qa:new")
    assert hit.provenance == "official"
    assert hit.authority == 45


@pytest.mark.asyncio
async def test_the_leg_is_inert_when_no_index_is_configured() -> None:
    """A runtime without the lexical projection behaves exactly as before."""
    store = await _store(_record("qa:a", [1.0, 0.0]))
    service = RetrievalService(
        embedder=FakeEmbedder(),
        vectors=store,
        lexical=None,
        qa_top_k=5,
        message_top_k=2,
    )

    retrieved = await service.retrieve(QUESTION)

    assert retrieved.lexical_qa == []
    assert [item.source_id for item in retrieved.qa] == ["qa:a"]


def test_the_merge_keeps_one_copy_of_an_item_found_twice() -> None:
    """Two near-identical sources is the shape the generator already fails."""
    semantic = [
        VectorMatch(id="qa:a", score=0.60, metadata={}),
        VectorMatch(id="qa:b", score=0.55, metadata={}),
    ]
    lexical = [
        VectorMatch(id="qa:a", score=5.0, metadata={}),
        VectorMatch(id="qa:c", score=4.0, metadata={}),
    ]

    merged = _merge_lexical(semantic, lexical)

    assert [match.id for match in merged] == ["qa:a", "qa:b", "qa:c"]


def test_the_merge_keeps_the_semantic_copy_of_a_duplicated_item() -> None:
    """The kept score must be the cosine the floor and the gate use."""
    semantic = [VectorMatch(id="qa:a", score=0.60, metadata={})]
    lexical = [VectorMatch(id="qa:a", score=5.0, metadata={})]

    merged = _merge_lexical(semantic, lexical)

    assert len(merged) == 1
    assert merged[0].score == 0.60
