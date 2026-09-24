# SPDX-License-Identifier: MIT
"""NumPy vector similarity stored in the local SQLite database."""

import json
from datetime import UTC, datetime
from typing import cast

import numpy as np

from knowledge_bot.infrastructure.local.database import (
    LocalDataError,
    SQLiteDatabase,
)
from knowledge_bot.ports.vector_store import VectorMatch, VectorRecord

_FILTER_COLUMNS = {"kind", "scope_key", "status"}


class NumpySqliteVectorStore:
    """A small local vector projection backed by SQLite and NumPy."""

    def __init__(self, database: SQLiteDatabase) -> None:
        """Use the shared local SQLite connection."""
        self._database = database

    async def upsert(self, records: list[VectorRecord]) -> None:
        """Insert or replace normalized vectors."""
        if not records:
            return
        connection = self._database.connection
        async with self._database.transaction():
            for record in records:
                values = self._normalize(record.values)
                metadata = record.metadata
                kind = self._required(metadata, "kind")
                scope_key = self._required(metadata, "scope_key")
                object_id = self._required(metadata, "object_id")
                status = metadata.get("status")
                canonical_key = metadata.get("canonical_key")
                await connection.execute(
                    "INSERT INTO local_vectors"
                    " (vector_id, kind, scope_key, status, canonical_key, object_id,"
                    " dimensions, embedding, metadata_json, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(vector_id) DO UPDATE SET"
                    " kind=excluded.kind, scope_key=excluded.scope_key,"
                    " status=excluded.status, canonical_key=excluded.canonical_key,"
                    " object_id=excluded.object_id, dimensions=excluded.dimensions,"
                    " embedding=excluded.embedding,"
                    " metadata_json=excluded.metadata_json,"
                    " updated_at=excluded.updated_at",
                    (
                        record.id,
                        kind,
                        scope_key,
                        None if status is None else str(status),
                        None if canonical_key is None else str(canonical_key),
                        object_id,
                        len(values),
                        values.tobytes(),
                        json.dumps(metadata, separators=(",", ":"), default=str),
                        datetime.now(UTC).isoformat(),
                    ),
                )

    async def query(
        self,
        vector: list[float],
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[VectorMatch]:
        """Return cosine-similarity matches from the local projection."""
        if top_k < 1:
            raise LocalDataError("top_k must be at least 1")  # noqa: TRY003
        query = self._normalize(vector)
        clauses: list[str] = []
        parameters: list[object] = []
        for key, value in (filters or {}).items():
            if key not in _FILTER_COLUMNS:
                raise LocalDataError(f"unsupported local vector filter: {key}")  # noqa: TRY003
            clauses.append(f"{key} = ?")
            parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self._database.connection.execute(
            "SELECT vector_id, dimensions, embedding, metadata_json"
            f" FROM local_vectors{where}",
            parameters,
        )
        rows = list(await cursor.fetchall())
        if not rows:
            return []
        if any(int(row[1]) != len(query) for row in rows):
            raise LocalDataError("local vector dimensions do not match query")  # noqa: TRY003
        matrix = np.vstack(
            [np.frombuffer(row[2], dtype=np.float32, count=int(row[1])) for row in rows]
        )
        scores = matrix @ query
        order = np.argsort(-scores)[:top_k]
        return [
            VectorMatch(
                id=str(rows[index][0]),
                score=float(scores[index]),
                metadata=cast(dict, json.loads(rows[index][3])),
            )
            for index in order
        ]

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors by their stable projection ids."""
        if not ids:
            return
        async with self._database.transaction() as connection:
            await connection.executemany(
                "DELETE FROM local_vectors WHERE vector_id = ?",
                [(vector_id,) for vector_id in ids],
            )

    async def clear(self) -> None:
        """Delete the derived local projection."""
        async with self._database.transaction() as connection:
            await connection.execute("DELETE FROM local_vectors")

    @staticmethod
    def _normalize(values: list[float]) -> np.ndarray:
        """Normalize one vector and reject zero-norm input."""
        array = np.asarray(values, dtype=np.float32)
        if array.ndim != 1 or not len(array):
            raise LocalDataError(  # noqa: TRY003
                "vectors must be non-empty one-dimensional values"
            )
        norm = float(np.linalg.norm(array))
        if norm == 0:
            raise LocalDataError("zero-norm vectors cannot be indexed")  # noqa: TRY003
        return array / norm

    @staticmethod
    def _required(metadata: dict[str, object], key: str) -> str:
        """Read a required metadata string."""
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise LocalDataError(f"vector metadata requires {key}")  # noqa: TRY003
        return value
