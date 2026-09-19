# SPDX-License-Identifier: MIT
"""Normalized inbound message contracts (spec §5)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from knowledge_bot.domain.enums import ContentType, ProcessingStatus


class AttachmentRef(BaseModel):
    """Reference to an attachment; binary content is never downloaded in v1."""

    kind: str
    external_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    processing_status: ProcessingStatus = ProcessingStatus.UNPROCESSED


class NormalizedMessage(BaseModel):
    """Channel-independent message produced by every inbound adapter."""

    id: str
    source_type: Literal["telegram", "whatsapp", "web"]
    conversation_id: str
    sender_is_admin: bool
    timestamp: datetime
    content_type: ContentType
    source_message_id: str | None = None
    sender_id: str | None = None
    text: str | None = None
    reply_to_message_id: str | None = None
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
