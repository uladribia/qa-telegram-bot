# SPDX-License-Identifier: MIT
"""Channel-independent inbound message model (spec §5).

Every inbound connector produces this one model, so the application layer has a
single message shape to reason about regardless of which channel delivered it.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from knowledge_bot.domain.enums import ContentType
from knowledge_bot.models.common import AttachmentRef, SourceDescriptor


class MemberChange(StrEnum):
    """A principal's arrival at, or departure from, a conversation."""

    JOINED = "joined"
    LEFT = "left"


class ConversationMemberEvent(BaseModel):
    """One membership change the channel reported with this message.

    Channels report membership in many shapes: Telegram sends join and leave
    service messages, other connectors may report it inline. The adapter
    reduces all of them to this, so the core never learns a channel's own
    vocabulary for who came and went.
    """

    principal_id: str
    change: MemberChange


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
    #: Membership changes the channel reported alongside this message. Empty
    #: for ordinary traffic; a service message that joins or leaves people is
    #: still a message, and the events travel with it.
    member_events: list[ConversationMemberEvent] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
