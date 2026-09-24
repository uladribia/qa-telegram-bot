# SPDX-License-Identifier: MIT
"""Port for the derived lexical (BM25/FTS5) search projection."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LexicalRecord:
    """One searchable text row keyed by its stable vector id."""

    id: str
    text: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LexicalMatch:
    """One lexical hit in best-first order."""

    id: str
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class LexicalIndex(Protocol):
    """A derived, rebuildable BM25 projection of the same records."""

    async def upsert(self, records: list[LexicalRecord]) -> None:
        """Insert or replace lexical rows."""
        ...

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        filters: dict[str, object] | None = None,
    ) -> list[LexicalMatch]:
        """Return BM25-ranked matches, optionally filtered by metadata."""
        ...

    async def delete(self, ids: list[str]) -> None:
        """Delete lexical rows by their stable projection ids."""
        ...
