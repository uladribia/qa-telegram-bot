# SPDX-License-Identifier: MIT
"""Integration tests for the seed service (in-memory fakes)."""

from datetime import UTC, datetime
from typing import Literal

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.seed import SeedService
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.enums import ContentType, QAStatus
from tests.fakes.repositories import (
    InMemoryAttachmentRepository,
    InMemoryConversationRepository,
    InMemoryMessageRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
    InMemorySourceRepository,
)
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _service() -> tuple[
    SeedService, InMemoryQAItemRepository, InMemoryQAVersionRepository
]:
    qa_items = InMemoryQAItemRepository()
    qa_versions = InMemoryQAVersionRepository()
    ingestor = MessageIngestor(
        sources=InMemorySourceRepository(),
        conversations=InMemoryConversationRepository(),
        messages=InMemoryMessageRepository(),
        attachments=InMemoryAttachmentRepository(),
    )
    service = SeedService(
        qa_items=qa_items,
        qa_versions=qa_versions,
        sources=InMemorySourceRepository(),
        ingestor=ingestor,
        clock=FrozenClock(NOW),
    )
    return service, qa_items, qa_versions


def _entry(
    anchor: str,
    *,
    status: Literal["published", "in_review"] = "published",
    question: str = "P?",
) -> SeedQA:
    return SeedQA(
        source_url="https://example.com/",
        source_anchor=anchor,
        section="S",
        question=question,
        answer="R.",
        status=status,
        retrieved_at=NOW,
    )


async def test_seed_qa_creates_item_and_version() -> None:
    """A published entry becomes an active item with a version."""
    service, items, versions = _service()
    created, skipped = await service.seed_qa([_entry("qa-1")])
    assert (created, skipped) == (1, 0)
    item = await items.get_by_canonical_key("qa-1")
    assert item is not None
    assert item.status is QAStatus.ACTIVE
    version_id = item.current_version_id
    assert version_id is not None
    version = await versions.get(version_id)
    assert version is not None
    assert version.answer == "R."
    assert version.authority == 90


async def test_in_review_entries_are_under_review_with_low_authority() -> None:
    """In-review entries are stored but cannot be retrieved as active."""
    service, items, versions = _service()
    await service.seed_qa([_entry("qa-2", status="in_review")])
    item = await items.get_by_canonical_key("qa-2")
    assert item is not None
    assert item.status is QAStatus.UNDER_REVIEW
    version_id = item.current_version_id
    assert version_id is not None
    version = await versions.get(version_id)
    assert version is not None
    assert version.authority == 30


async def test_seeding_is_idempotent() -> None:
    """Re-seeding the same snapshot skips existing entries."""
    service, _, _ = _service()
    await service.seed_qa([_entry("qa-1")])
    created, skipped = await service.seed_qa([_entry("qa-1")])
    assert (created, skipped) == (0, 1)


async def test_seed_messages_uses_ingest_idempotency() -> None:
    """Imported messages are deduplicated by the ingest idempotency key."""
    service, _, _ = _service()
    message = NormalizedMessage(
        id="m1",
        source_type="whatsapp",
        conversation_id="wa",
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        source_message_id="wa:1",
        text="hola",
    )
    assert await service.seed_messages([message]) == 1
    assert await service.seed_messages([message]) == 0
