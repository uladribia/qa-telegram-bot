# SPDX-License-Identifier: MIT
"""Vector store port."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """A vector and its metadata to upsert."""

    id: str
    values: list[float]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """A vector query result."""

    id: str
    score: float
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """A derived, rebuildable semantic index."""

    async def upsert(self, records: list[VectorRecord]) -> None:
        """Insert or replace vectors."""
        ...

    async def query(
        self,
        vector: list[float],
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[VectorMatch]:
        """Return the closest vectors, optionally filtered by metadata."""
        ...

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors by id."""
        ...
