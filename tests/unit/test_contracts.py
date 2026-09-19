# SPDX-License-Identifier: MIT
"""Tests for the boundary contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from knowledge_bot.contracts.messages import AttachmentRef, NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.enums import ContentType, ProcessingStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_normalized_message_defaults() -> None:
    """Optional collections default to empty and admin defaults are explicit."""
    message = NormalizedMessage(
        id="m1",
        source_type="telegram",
        conversation_id="c1",
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        text="hola",
    )
    assert message.attachments == []
    assert message.metadata == {}
    assert message.reply_to_message_id is None


def test_attachment_ref_defaults_to_unprocessed() -> None:
    """Attachments are never processed in v1 unless something changes them."""
    attachment = AttachmentRef(kind="image")
    assert attachment.processing_status is ProcessingStatus.UNPROCESSED


def test_attachment_ref_accepts_metadata_only() -> None:
    """Image metadata is captured without any binary content."""
    attachment = AttachmentRef(kind="image", width=1080, height=720, size_bytes=2048)
    assert attachment.width == 1080
    assert attachment.file_name is None


def test_normalized_message_rejects_unknown_source_type() -> None:
    """Only known channel literals are accepted."""
    with pytest.raises(ValidationError):
        NormalizedMessage.model_validate(
            {
                "id": "m1",
                "source_type": "carrier-pigeon",
                "conversation_id": "c1",
                "sender_is_admin": False,
                "timestamp": NOW,
                "content_type": ContentType.TEXT,
            }
        )


def test_seed_qa_rejects_unknown_status() -> None:
    """Seed entries are either published or in review."""
    with pytest.raises(ValidationError):
        SeedQA.model_validate(
            {
                "source_url": "https://example.com",
                "section": "Equipament",
                "question": "?",
                "answer": "!",
                "status": "draft",
                "retrieved_at": NOW,
            }
        )
