# SPDX-License-Identifier: MIT
"""The lexical projection against real SQLite and the real migrations.

The unit tests drive the gates with fakes. These apply every migration to a real
database and exercise the FTS5 write and read, because a column name, a
tokenizer option or a batch shape that a fake cannot see is exactly the kind of
mistake that only appears against the engine.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.infrastructure.local.database import SQLiteDatabase, apply_migrations
from knowledge_bot.infrastructure.local.sqlite_repositories import SQLiteBinding
from knowledge_bot.infrastructure.local.vector_store import NumpySqliteVectorStore
from knowledge_bot.infrastructure.sql.lexical import (
    SqlLexicalIndex,
    lexical_match_query,
)
from knowledge_bot.infrastructure.sql.repositories import SqlSearchProjectionRepository
from knowledge_bot.ports.index import IndexableQA, LexicalRecord
from tests.fakes.ai import (
    FakeEmbedder,
    FakeSearchIndexSource,
    InMemorySearchProjectionRepository,
)
from tests.fakes.support import FrozenClock

pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 1, tzinfo=UTC)

QA = IndexableQA(
    qa_item_id="qa-one",
    version_id="v1",
    question="Què inclou el pagament de la inscripció?",
    answer="La quota del club i la llicència federativa, pagades amb Cluber.",
    authority=90,
    canonical_key="qa-one",
    source_anchor="qa-one",
    url="https://example.org",
)


async def _database(tmp_path: Path) -> SQLiteDatabase:
    """Open a database with every migration applied."""
    database = await SQLiteDatabase.connect(tmp_path / "knowledge.sqlite3")
    await apply_migrations(database, Path.cwd())
    return database


def _binding(database: SQLiteDatabase) -> SQLiteBinding:
    """Expose the shared SQL execution protocol to SQLite."""
    return SQLiteBinding(database)


@pytest.mark.asyncio
async def test_projection_writes_the_lexical_row_beside_the_vector(
    tmp_path: Path,
) -> None:
    """One projection call leaves a vector and a searchable answer row."""
    database = await _database(tmp_path)
    try:
        vectors = NumpySqliteVectorStore(database)
        lexical = SqlLexicalIndex(_binding(database))
        service = SearchProjectionService(
            source=FakeSearchIndexSource(qa=[QA]),
            embedder=FakeEmbedder([1.0, 0.0]),
            vectors=vectors,
            manifest=InMemorySearchProjectionRepository(),
            clock=FrozenClock(NOW),
            lexical=lexical,
        )

        await service.project_qa(QA)

        matches = await lexical.search(
            "cluber", top_k=5, filters={"kind": "qa", "scope_key": "global"}
        )
        assert [match.id for match in matches] == ["qa:qa-one"]
        assert "Cluber" in str(matches[0].metadata["text"])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_removing_a_projection_removes_the_lexical_row(tmp_path: Path) -> None:
    """The two projections share a lifecycle, so deletion covers both."""
    database = await _database(tmp_path)
    try:
        lexical = SqlLexicalIndex(_binding(database))
        service = SearchProjectionService(
            source=FakeSearchIndexSource(qa=[QA]),
            embedder=FakeEmbedder([1.0, 0.0]),
            vectors=NumpySqliteVectorStore(database),
            manifest=InMemorySearchProjectionRepository(),
            clock=FrozenClock(NOW),
            lexical=lexical,
        )
        await service.project_qa(QA)

        await service.remove(["qa:qa-one"])

        assert await lexical.search("cluber", top_k=5) == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_the_indexed_answer_is_reachable_by_an_accented_query(
    tmp_path: Path,
) -> None:
    """The table strips diacritics, so the query must fold them too."""
    database = await _database(tmp_path)
    try:
        await SqlLexicalIndex(_binding(database)).upsert(
            [
                LexicalRecord(
                    id="qa:qa-two",
                    kind="qa",
                    scope_key="global",
                    canonical_key="qa-two",
                    authority=90,
                    text="La llicència federativa va pel Cluber.",
                    metadata={"text": "La llicència federativa va pel Cluber."},
                )
            ]
        )
        lexical = SqlLexicalIndex(_binding(database))

        folded = await lexical.search("llicència", top_k=5)
        unfolded = await lexical.search("llicència", top_k=5)

        assert [match.id for match in folded] == ["qa:qa-two"]
        assert len(unfolded) == len(folded)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_a_query_of_only_stopwords_matches_nothing(tmp_path: Path) -> None:
    """No content token means no query, not a match on everything."""
    database = await _database(tmp_path)
    try:
        await SqlLexicalIndex(_binding(database)).upsert(
            [
                LexicalRecord(
                    id="qa:qa-three",
                    kind="qa",
                    scope_key="global",
                    canonical_key="qa-three",
                    authority=90,
                    text="El camp és al Pau Negre.",
                    metadata={},
                )
            ]
        )
        lexical = SqlLexicalIndex(_binding(database))

        assert lexical_match_query("de la el que i a en") == ""
        assert await lexical.search("de la el que i a en", top_k=5) == []
        assert [m.id for m in await lexical.search("on és el camp?", top_k=5)] == [
            "qa:qa-three"
        ]
    finally:
        await database.close()


async def _drift(database: SQLiteDatabase) -> tuple[list[str], list[str]]:
    """Return the vector-only and lexical-only ids of the Q&A projections.

    The two projections are written together from the same metadata, so a
    corpus that has been fully projected must have no ids on one side only.
    An id on the left was indexed without its answer text, which makes the
    lexical leg silently blind to it; an id on the right has no vector and is
    invisible to semantic retrieval.
    """
    cursor = await database.connection.execute(
        "SELECT vector_id FROM search_projection WHERE kind = 'qa' "
        "AND vector_id NOT IN (SELECT vector_id FROM search_fts)"
    )
    vector_only = sorted({row[0] for row in await cursor.fetchall()})
    cursor = await database.connection.execute(
        "SELECT vector_id FROM search_fts WHERE kind = 'qa' "
        "AND vector_id NOT IN (SELECT vector_id FROM search_projection)"
    )
    lexical_only = sorted({row[0] for row in await cursor.fetchall()})
    return vector_only, lexical_only


async def _service(
    database: SQLiteDatabase, corpus: list[IndexableQA]
) -> SearchProjectionService:
    """Build a projection service writing to real SQL on both sides.

    The manifest must be the real repository, not the in-memory fake: the
    invariant under test is about the ``search_projection`` table, and a fake
    manifest would leave that table empty and make every id look lexical-only.
    """
    return SearchProjectionService(
        source=FakeSearchIndexSource(qa=corpus),
        embedder=FakeEmbedder([1.0, 0.0]),
        vectors=NumpySqliteVectorStore(database),
        manifest=SqlSearchProjectionRepository(_binding(database)),
        clock=FrozenClock(NOW),
        lexical=SqlLexicalIndex(_binding(database)),
    )


@pytest.mark.asyncio
async def test_a_projected_corpus_leaves_no_drift_between_the_projections(
    tmp_path: Path,
) -> None:
    """Projecting the whole corpus populates both projections equally.

    This is the invariant that production violated: the club corpus was
    vector-indexed before the FTS table existed, so it was never projected
    lexically, and nothing detected it. Every Q&A id must appear in both.
    """
    database = await _database(tmp_path)
    try:
        corpus = [
            IndexableQA(
                qa_item_id=f"qa-{index}",
                version_id=f"v{index}",
                question=f"Pregunta {index}?",
                answer=f"Resposta {index} amb el Camp del Pau Negre.",
                authority=90,
                canonical_key=f"qa-{index}",
                source_anchor=f"qa-{index}",
                url="https://example.org",
            )
            for index in range(5)
        ]
        service = await _service(database, corpus)

        for item in corpus:
            await service.project_qa(item)

        assert await _drift(database) == ([], [])
        matches = await SqlLexicalIndex(_binding(database)).search(
            "pau negre", top_k=10, filters={"kind": "qa"}
        )
        assert len(matches) == 5
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_drift_is_detected_when_a_lexical_row_is_missing(
    tmp_path: Path,
) -> None:
    """The audit must actually notice a one-sided projection.

    A drift check that cannot fail is worse than none, so this deletes the
    lexical row the way the pre-0024 corpus looks and asserts it is reported.
    """
    database = await _database(tmp_path)
    try:
        service = await _service(database, [QA])

        await service.project_qa(QA)
        assert await _drift(database) == ([], [])

        await database.connection.execute(
            "DELETE FROM search_fts WHERE vector_id = ?", ("qa:qa-one",)
        )

        assert await _drift(database) == (["qa:qa-one"], [])
    finally:
        await database.close()
