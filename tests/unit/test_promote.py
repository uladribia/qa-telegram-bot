# SPDX-License-Identifier: MIT
"""Unit tests for publishing a seeded under_review Q&A item."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from knowledge_bot.application.promote import QAPromoter
from knowledge_bot.domain.entities import QAItem
from knowledge_bot.domain.enums import QAStatus
from knowledge_bot.domain.identity import canonical_key_for
from knowledge_bot.ports.repositories import QAItemRepository, QAVersionRepository
from tests.fakes.support import FrozenClock

NOW = FrozenClock(datetime(2026, 9, 27, 20, 0, tzinfo=UTC))


class _Items:
    """Minimal in-memory Q&A item store."""

    def __init__(self, *items: QAItem) -> None:
        self.items = {item.id: item for item in items}
        self.saved: list[QAItem] = []

    async def add(self, item: QAItem) -> None:
        self.items[item.id] = item

    async def get(self, qa_id: str) -> QAItem | None:
        return self.items.get(qa_id)

    async def get_by_canonical_key(
        self, canonical_key: str, scope_key: str = "global"
    ) -> QAItem | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.canonical_key == canonical_key and item.scope_key == scope_key
            ),
            None,
        )

    async def save(self, item: QAItem) -> None:
        self.items[item.id] = item
        self.saved.append(item)


class _Versions:
    """Unused by promotion, present to satisfy the port."""

    async def add(self, version: object) -> None: ...

    async def get(self, version_id: str) -> None:
        return None


def _item(qa_id: str, question: str, status: QAStatus) -> QAItem:
    return QAItem(
        id=qa_id,
        canonical_key=canonical_key_for(question),
        canonical_question=question,
        status=status,
        created_at=NOW.now(),
        updated_at=NOW.now(),
        current_version_id=f"qav-{qa_id}-1",
    )


def _promoter(items: _Items) -> QAPromoter:
    return QAPromoter(
        qa_items=cast_items(items),
        qa_versions=cast_versions(_Versions()),
        clock=NOW,
    )


def cast_items(items: _Items) -> QAItemRepository:
    """Return the store, typed as the port."""
    return items  # type: ignore[return-value]


def cast_versions(versions: _Versions) -> QAVersionRepository:
    """Return the version store, typed as the port."""
    return versions  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_promoting_an_under_review_item_activates_it() -> None:
    """A seeded in_review item becomes retrievable after promotion."""
    store = _Items(_item("qa-1", "Com s'escull el dorsal?", QAStatus.UNDER_REVIEW))
    promoter = _promoter(store)

    promoted = await promoter.promote("qa-1")

    assert promoted is not None
    assert promoted.status is QAStatus.ACTIVE
    assert store.items["qa-1"].status is QAStatus.ACTIVE
    assert store.saved[-1].status is QAStatus.ACTIVE


@pytest.mark.asyncio
async def test_promoting_by_question_text_works() -> None:
    """The operator can paste the question the review report shows."""
    store = _Items(_item("qa-1", "Com s'escull el dorsal?", QAStatus.UNDER_REVIEW))
    promoter = _promoter(store)

    promoted = await promoter.promote("Com s'escull el dorsal?")

    assert promoted is not None
    assert promoted.id == "qa-1"


@pytest.mark.asyncio
async def test_promotion_is_idempotent() -> None:
    """Re-running the command does not rewrite an already active item."""
    store = _Items(_item("qa-1", "On és el camp?", QAStatus.ACTIVE))
    promoter = _promoter(store)

    promoted = await promoter.promote("qa-1")

    assert promoted is not None
    assert promoted.status is QAStatus.ACTIVE
    assert store.saved == []


@pytest.mark.asyncio
async def test_an_unknown_target_promotes_nothing() -> None:
    """A typo is reported instead of silently succeeding."""
    promoter = _promoter(_Items())

    assert await promoter.promote("qa-nope") is None
    assert await promoter.promote("Cap pregunta així") is None
    assert await promoter.promote("   ") is None


@pytest.mark.asyncio
async def test_promotion_does_not_touch_the_answer() -> None:
    """Only the status moves: no version is rewritten by a promotion."""
    original = _item("qa-1", "On és el camp?", QAStatus.UNDER_REVIEW)
    store = _Items(original)
    promoter = _promoter(store)

    promoted = await promoter.promote("qa-1")

    assert promoted is not None
    assert promoted.current_version_id == original.current_version_id
    assert replace(promoted, status=QAStatus.UNDER_REVIEW).canonical_key == (
        original.canonical_key
    )
