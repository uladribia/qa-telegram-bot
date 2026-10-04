# SPDX-License-Identifier: MIT
"""Unit tests for the Vectorize adapter's id handling."""

import pytest

from knowledge_bot.infrastructure.cloudflare.vectorize import VectorizeStore


class _OverLongIdError(ValueError):
    """Stands in for the platform's VECTOR_DELETE_ERROR rejection."""

    def __init__(self, size: int) -> None:
        """Report the offending id size against the platform limit.

        Args:
            size: The id length in bytes that was rejected.
        """
        super().__init__(f"id too long; max is 64 bytes, got {size}")


class _RecordingIndex:
    """A Vectorize binding that records the ids it was asked to delete."""

    def __init__(self, reject_long: bool = True) -> None:
        """Record deletions, optionally enforcing the platform byte limit.

        Args:
            reject_long: Raise the platform error on an over-long id, exactly
                as the real binding does with ``VECTOR_DELETE_ERROR``.
        """
        self.deleted: list[list[str]] = []
        self.reject_long = reject_long

    async def upsert(self, vectors: list[dict[str, object]]) -> object:
        """Accept an upsert."""
        return None

    async def query(self, vector: list[float], options: dict[str, object]) -> object:
        """Return no matches."""
        return {"matches": []}

    async def deleteByIds(self, ids: list[str]) -> object:
        """Record the batch, rejecting an over-long id when configured.

        Args:
            ids: The ids the store asked to delete.

        Returns:
            ``None``.

        Raises:
            _OverLongIdError: When an id is over the platform limit and the
                fake is configured to reject it.
        """
        for vector_id in ids:
            size = len(vector_id.encode("utf-8"))
            if self.reject_long and size > 64:
                raise _OverLongIdError(size)
        self.deleted.append(list(ids))
        return None


@pytest.mark.asyncio
async def test_an_id_past_the_platform_limit_is_never_sent_for_deletion() -> None:
    """An over-long id cannot exist in Vectorize, so it is dropped.

    Production carried one 76-byte legacy Q&A version id. Sending it made
    ``deleteByIds`` reject the whole batch with a 40008, which surfaced as a
    500 on the cleanup endpoint and blocked every reindex.
    """
    index = _RecordingIndex()
    store = VectorizeStore(index)
    oversized = "qav:qa-a41f9accb6ce836e:space:sp_" + "0" * 32 + ":1790632397"

    await store.delete([oversized, "qa:qa-one"])

    assert index.deleted == [["qa:qa-one"]]


@pytest.mark.asyncio
async def test_a_batch_of_only_oversized_ids_sends_nothing() -> None:
    """Nothing deletable means no call, not an empty call that may fail."""
    index = _RecordingIndex()
    store = VectorizeStore(index)

    await store.delete(["x" * 65, "y" * 200])

    assert index.deleted == []


@pytest.mark.asyncio
async def test_ordinary_ids_are_deleted_unchanged() -> None:
    """The limit must not truncate or drop legitimate ids."""
    index = _RecordingIndex()
    store = VectorizeStore(index)
    at_limit = "a" * 64

    await store.delete(["qa:qa-one", at_limit])

    assert index.deleted == [["qa:qa-one", at_limit]]


@pytest.mark.asyncio
async def test_the_limit_counts_bytes_not_characters() -> None:
    """Multi-byte characters count as bytes, which is what the platform counts."""
    index = _RecordingIndex()
    store = VectorizeStore(index)
    # 30 two-byte characters is 60 bytes but 30 characters.
    multibyte = "é" * 30

    await store.delete([multibyte, "é" * 33])

    assert index.deleted == [[multibyte]]
