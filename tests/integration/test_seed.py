# SPDX-License-Identifier: MIT
"""Integration tests for the seed service (in-memory fakes)."""

from datetime import UTC, datetime
from typing import Literal

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.seed import SeedService
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.entities import QAItem, QAVersion
from knowledge_bot.domain.enums import ContentType, QAOrigin, QAStatus
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
    answer: str = "R.",
) -> SeedQA:
    return SeedQA(
        source_url="https://example.com/",
        source_anchor=anchor,
        section="S",
        question=question,
        answer=answer,
        status=status,
        retrieved_at=NOW,
    )


async def test_seed_qa_creates_item_and_version() -> None:
    """A published entry becomes an active item with a version."""
    service, items, versions = _service()
    created, skipped, _, _ = await service.seed_qa([_entry("qa-1")])
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
    created, skipped, _, _ = await service.seed_qa([_entry("qa-1")])
    assert (created, skipped) == (0, 1)


async def test_seed_returns_version_ids_needing_indexing() -> None:
    """Created and renewed versions are reported for incremental indexing."""
    service, items, versions = _service()
    created, _, _, first_ids = await service.seed_qa([_entry("qa-1")])
    assert created == 1
    item = await items.get_by_canonical_key("qa-1")
    assert item is not None and item.current_version_id in first_ids
    created, _, renewed, second_ids = await service.seed_qa(
        [_entry("qa-1", answer="R v2.")], renew=True
    )
    assert (created, renewed) == (0, 1)
    assert second_ids != first_ids
    updated = await items.get_by_canonical_key("qa-1")
    assert updated is not None and updated.current_version_id in second_ids
    assert await versions.get(first_ids[0]) is not None


async def test_seed_qa_is_scoped_per_group() -> None:
    """A group-scoped seed coexists with the global entry of the same key."""
    service, items, _ = _service()
    created, _, _, _ = await service.seed_qa([_entry("qa-1")], scope="-100")
    assert created == 1
    item = await items.get_by_canonical_key("qa-1", "-100")
    assert item is not None
    assert item.scope == "-100"
    # The global scope stays untouched and re-seeding is still idempotent.
    assert await items.get_by_canonical_key("qa-1") is None
    created, skipped, _, _ = await service.seed_qa([_entry("qa-1")], scope="-100")
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


async def test_renew_updates_changed_entries_append_only() -> None:
    """Renewal creates a newer version; unchanged entries are skipped."""
    service, items, versions = _service()
    await service.seed_qa([_entry("qa-1", question="Què?,", answer="R v1.")])
    item = await items.get_by_canonical_key("qa-1")
    assert item is not None
    first_version_id = item.current_version_id
    assert first_version_id is not None

    # Unchanged entry: skipped even with renew.
    created, skipped, renewed, _ = await service.seed_qa(
        [_entry("qa-1", question="Què?,", answer="R v1.")], renew=True
    )
    assert (created, skipped, renewed) == (0, 1, 0)

    # Changed answer: new version wins, the old one is kept.
    created, skipped, renewed, _ = await service.seed_qa(
        [_entry("qa-1", question="Què?,", answer="R v2.")], renew=True
    )
    assert (created, skipped, renewed) == (0, 0, 1)
    renewed_item = await items.get_by_canonical_key("qa-1")
    assert renewed_item is not None
    current_version_id = renewed_item.current_version_id
    assert current_version_id is not None
    assert current_version_id != first_version_id
    new_version = await versions.get(current_version_id)
    assert new_version is not None
    assert new_version.answer == "R v2."
    assert new_version.supersedes_version_id == first_version_id
    assert await versions.get(first_version_id) is not None


async def test_renewed_web_beats_an_older_approved_correction() -> None:
    """A web renewal after a correction supersedes it: latest update wins."""
    service, items, versions = _service()
    key = "qa-1"
    item = QAItem(
        id="qa-item-1",
        canonical_key=key,
        canonical_question="P?",
        status=QAStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        current_version_id="qav-correction",
    )
    await items.add(item)
    await versions.add(
        QAVersion(
            id="qav-correction",
            qa_id="qa-item-1",
            answer="Correcció aprovada.",
            authority=100,
            origin=QAOrigin.ADMIN_APPROVED,
            created_at=NOW,
        )
    )
    created, skipped, renewed, _ = await service.seed_qa(
        [_entry(key, question="P?", answer="Web renovat.")], renew=True
    )
    assert (created, skipped, renewed) == (0, 0, 1)
    updated = await items.get("qa-item-1")
    assert updated is not None
    current_id = updated.current_version_id
    assert current_id is not None
    current = await versions.get(current_id)
    assert current is not None
    assert current.answer == "Web renovat."
    assert current.supersedes_version_id == "qav-correction"
    # The correction stays in history.
    assert await versions.get("qav-correction") is not None
