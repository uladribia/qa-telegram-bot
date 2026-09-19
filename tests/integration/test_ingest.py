# SPDX-License-Identifier: MIT
"""Integration tests for the idempotent ingest use case (in-memory fakes)."""

from datetime import UTC, datetime

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.contracts.messages import AttachmentRef, NormalizedMessage
from knowledge_bot.domain.enums import ContentType, SourceType
from tests.fakes.repositories import (
    InMemoryAttachmentRepository,
    InMemoryConversationRepository,
    InMemoryMessageRepository,
    InMemorySourceRepository,
)

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)


def _ingestor() -> MessageIngestor:
    return MessageIngestor(
        sources=InMemorySourceRepository(),
        conversations=InMemoryConversationRepository(),
        messages=InMemoryMessageRepository(),
        attachments=InMemoryAttachmentRepository(),
    )


def _message(
    message_id: str = "tg:-100:10",
    *,
    external_id: str = "-100:10",
    attachments: list[AttachmentRef] | None = None,
    is_admin: bool = False,
) -> NormalizedMessage:
    return NormalizedMessage(
        id=message_id,
        source_type="telegram",
        conversation_id="-100",
        sender_is_admin=is_admin,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        source_message_id=external_id,
        sender_id="abc123",
        text="Quan entrenen?",
        attachments=attachments or [],
    )


async def test_ingest_creates_source_conversation_and_message() -> None:
    """A first message creates the channel rows it depends on."""
    ingestor = _ingestor()
    result = await ingestor.ingest(_message())
    assert result.created is True
    source = await ingestor.sources.get(SourceType.TELEGRAM.value)
    assert source is not None
    assert source.source_type is SourceType.TELEGRAM
    conversation = await ingestor.conversations.get("-100")
    assert conversation is not None
    stored = await ingestor.messages.get("tg:-100:10")
    assert stored is not None
    assert stored.text == "Quan entrenen?"


async def test_reprocessing_the_same_event_does_not_duplicate() -> None:
    """Idempotency: the same external id is stored once."""
    ingestor = _ingestor()
    assert (await ingestor.ingest(_message(message_id="a"))).created is True
    second = await ingestor.ingest(_message(message_id="b", external_id="-100:10"))
    assert second.created is False
    assert await ingestor.messages.get("b") is None
    existing = await ingestor.messages.get_by_external_id(
        SourceType.TELEGRAM.value, "-100:10"
    )
    assert existing is not None
    assert existing.id == "a"


async def test_attachments_are_persisted_as_metadata() -> None:
    """Attachment metadata is stored; no content field exists."""
    ingestor = _ingestor()
    attachment = AttachmentRef(
        kind="image", external_id="file-1", width=1280, height=720
    )
    await ingestor.ingest(_message(attachments=[attachment]))
    stored = await ingestor.attachments.list_for_message("tg:-100:10")
    assert len(stored) == 1
    assert stored[0].kind == "image"
    assert stored[0].external_file_id == "file-1"
    assert stored[0].processing_status.value == "unprocessed"


async def test_admin_flag_is_persisted() -> None:
    """The admin flag travels from the contract to the stored message."""
    ingestor = _ingestor()
    await ingestor.ingest(_message(is_admin=True))
    stored = await ingestor.messages.get("tg:-100:10")
    assert stored is not None
    assert stored.sender_is_admin is True
