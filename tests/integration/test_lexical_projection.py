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
