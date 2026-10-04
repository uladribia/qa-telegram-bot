# SPDX-License-Identifier: MIT
"""Vectorize-backed vector store.

``env.VECTORIZE`` is wrapped by the Workers runtime, so query results arrive as
Python data.
"""

from itertools import batched
from typing import Protocol, cast

from knowledge_bot.ports.vector_store import VectorMatch, VectorRecord

# Vectorize accepts at most 100 ids per delete call.
_DELETE_CHUNK = 100

#: Vectorize rejects any id longer than this with ``VECTOR_DELETE_ERROR``
#: (code 40008), which is a hard platform limit, not a quota. Such an id can
#: therefore never exist in the index, so asking to delete it is meaningless
#: and fails the whole batch. One legacy Q&A version id measured 76 bytes,
#: which made the projection-cleanup endpoint return 500 and blocked every
#: reindex: the id could not be deleted and the request could not proceed.
_MAX_ID_BYTES = 64


class VectorizeIndex(Protocol):
    """The subset of the Vectorize binding used here."""

    async def upsert(self, vectors: list[dict[str, object]]) -> object:
        """Insert or replace vectors."""
        ...

    async def query(self, vector: list[float], options: dict[str, object]) -> object:
        """Query the index."""
        ...

    async def deleteByIds(self, ids: list[str]) -> object:
        """Delete vectors by id."""
        ...


def _field(value: object, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


class VectorizeStore:
    """A vector store backed by a Cloudflare Vectorize index."""

    def __init__(self, index: VectorizeIndex) -> None:
        """Wrap a Vectorize index binding."""
        self._index = index

    async def upsert(self, records: list[VectorRecord]) -> None:
        """Insert or replace vectors."""
        if not records:
            return
        await self._index.upsert(
            [
                {"id": record.id, "values": record.values, "metadata": record.metadata}
                for record in records
            ]
        )

    async def query(
        self,
        vector: list[float],
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[VectorMatch]:
        """Return the closest vectors, optionally filtered by metadata."""
        options: dict[str, object] = {"topK": top_k, "returnMetadata": "all"}
        if filters is not None:
            options["filter"] = filters
        result = await self._index.query(vector, options)
        matches = _field(result, "matches")
        if not isinstance(matches, list):
            return []
        return [
            VectorMatch(
                id=str(_field(match, "id")),
                score=float(cast(float, _field(match, "score") or 0.0)),
                metadata=dict(cast(dict, _field(match, "metadata") or {})),
            )
            for match in matches
        ]

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors by id, in chunks the index accepts.

        Ids past the platform's byte limit are dropped rather than sent: they
        cannot be present in the index, and including one rejects the entire
        batch with a 500. Skipping is therefore not a silent partial delete.
        """
        deletable = [
            vector_id
            for vector_id in ids
            if len(vector_id.encode("utf-8")) <= _MAX_ID_BYTES
        ]
        for chunk in batched(deletable, _DELETE_CHUNK, strict=False):
            await self._index.deleteByIds(list(chunk))
