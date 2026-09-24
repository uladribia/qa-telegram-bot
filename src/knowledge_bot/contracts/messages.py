# SPDX-License-Identifier: MIT
"""Normalized inbound message contracts (spec §5)."""

from datetime import datetime

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


class SourceDescriptor(BaseModel):
    """Connector-declared identity and base authority for one source."""

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    authority: int = Field(ge=0, le=100)


class NormalizedMessage(BaseModel):
    """Channel-independent message produced by every inbound adapter.

    ``is_sender_allowed`` is false for a private message from someone who is
    neither the admin nor on the allowed list. Such a message may only continue
    if it replies to a prompt the bot itself sent; otherwise it is dropped
    before storage. Public bot usernames are discoverable, so an open DM would
    otherwise let anyone spend the shared free AI quota.
    """

    id: str
    source: SourceDescriptor
    conversation_id: str
    sender_is_admin: bool
    timestamp: datetime
    content_type: ContentType
    space_id: str | None = None
    principal_id: str | None = None
    sender_authority: int | None = Field(default=None, ge=0, le=100)
    source_message_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    text: str | None = None
    reply_to_message_id: str | None = None
    #: Raw Telegram user ids used only as routing keys (reviewer nomination,
    #: confirmation checks); never logged and never displayed as data.
    sender_user_id: str | None = None
    reply_to_user_id: str | None = None
    reply_to_user_name: str | None = None
    mentions_bot: bool = False
    is_reply_to_bot: bool = False
    is_direct_message: bool = False
    is_sender_allowed: bool = True
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
