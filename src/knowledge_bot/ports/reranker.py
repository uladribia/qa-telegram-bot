# SPDX-License-Identifier: MIT
"""Cross-encoder reranking port."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Reranker(Protocol):
    """Scores documents against a question with a cross-encoder."""

    async def score(self, query: str, documents: list[str]) -> list[float]:
        """Score each document against the query, preserving input order.

        Raises:
            ModelUnavailableError: When the reranker call fails.
        """
        ...
