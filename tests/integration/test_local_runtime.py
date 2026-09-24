# SPDX-License-Identifier: MIT
"""Integration coverage for the local SQLite runtime."""

from pathlib import Path

import pytest

from knowledge_bot.infrastructure.local.database import SQLiteDatabase, apply_migrations
from knowledge_bot.infrastructure.local.vector_store import NumpySqliteVectorStore
from knowledge_bot.ports.vector_store import VectorRecord

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_local_migrations_and_vector_cosine_search(tmp_path: Path) -> None:
    """Apply every local migration and query the derived NumPy projection."""
    database = await SQLiteDatabase.connect(tmp_path / "knowledge.sqlite3")
    try:
        await apply_migrations(database, Path.cwd())
        store = NumpySqliteVectorStore(database)
        await store.upsert(
            [
                VectorRecord(
                    id="qa:one",
                    values=[1.0, 0.0],
                    metadata={
                        "kind": "qa",
                        "status": "active",
                        "scope_key": "global",
                        "canonical_key": "one",
                        "object_id": "one",
                    },
                ),
                VectorRecord(
                    id="msg:two",
                    values=[0.0, 1.0],
                    metadata={
                        "kind": "message",
                        "status": "active",
                        "scope_key": "global",
                        "canonical_key": None,
                        "object_id": "two",
                    },
                ),
            ]
        )
        matches = await store.query(
            [1.0, 0.1],
            top_k=2,
            filters={"kind": "qa", "scope_key": "global"},
        )
        assert [match.id for match in matches] == ["qa:one"]
        assert matches[0].score > 0.9
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_local_vector_rejects_unsupported_filter(tmp_path: Path) -> None:
    """Reject filter keys outside the shared vector contract."""
    database = await SQLiteDatabase.connect(tmp_path / "knowledge.sqlite3")
    try:
        await apply_migrations(database, Path.cwd())
        store = NumpySqliteVectorStore(database)
        with pytest.raises(ValueError, match="unsupported local vector filter"):
            await store.query([1.0], top_k=1, filters={"secret": "value"})
    finally:
        await database.close()
