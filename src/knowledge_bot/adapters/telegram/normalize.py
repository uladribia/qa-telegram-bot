# SPDX-License-Identifier: MIT
"""Telegram update normalization: raw updates to channel-independent models.

The adapter makes no knowledge decisions. It verifies the chat, converts the
payload into the channel-independent contract, and records attachment metadata
only: binary content is never fetched in v1.
"""

from datetime import UTC, datetime

from knowledge_bot.adapters.telegram.identity import (
    TELEGRAM_RUNTIME_SOURCE_ID,
    TelegramIdentity,
    pseudonymize,
)
from knowledge_bot.adapters.telegram.models import (
    NormalizedCallback,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
)
from knowledge_bot.domain.enums import ContentType
from knowledge_bot.domain.identity import principal_id
from knowledge_bot.infrastructure.security import secrets_match
from knowledge_bot.models.common import AttachmentRef, SourceDescriptor
from knowledge_bot.models.messages import (
    ConversationMemberEvent,
    MemberChange,
    NormalizedMessage,
)


def is_valid_webhook_secret(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of the Telegram webhook secret header.

    Args:
        provided: The value of ``X-Telegram-Bot-Api-Secret-Token``.
        expected: The configured secret.

    Returns:
        ``True`` only when both are present and equal.
    """
    if not provided or not expected:
        return False
    return secrets_match(provided, expected)


def _display_name(sender: object) -> str | None:
    """Return a human display name for a Telegram sender, if any.

    Args:
        sender: The Telegram user, if any.

    Returns:
        The first and last name, or the ``@username``, or ``None``.
    """
    if sender is None:
        return None
    parts = [
        getattr(sender, "first_name", None),
        getattr(sender, "last_name", None),
    ]
    name = " ".join(str(part) for part in parts if part).strip()
    if name:
        return name
    username = getattr(sender, "username", None)
    return f"@{username}" if username else None


def _attachment_for(
    message: TelegramMessage,
) -> tuple[ContentType, AttachmentRef | None]:
    if message.photo:
        largest = max(message.photo, key=lambda size: size.width * size.height)
        attachment = AttachmentRef(
            kind="image",
            external_id=largest.file_id,
            width=largest.width,
            height=largest.height,
            size_bytes=largest.file_size,
        )
        return ContentType.IMAGE, attachment
    if message.document is not None:
        return ContentType.DOCUMENT, AttachmentRef(
            kind="document",
            external_id=message.document.file_id,
            file_name=message.document.file_name,
            mime_type=message.document.mime_type,
            size_bytes=message.document.file_size,
        )
    for media, kind in (
        (message.voice, "audio"),
        (message.audio, "audio"),
        (message.video, "video"),
    ):
        if media is not None:
            return ContentType(kind), AttachmentRef(
                kind=kind,
                external_id=media.file_id,
                file_name=media.file_name,
                mime_type=media.mime_type,
                size_bytes=media.file_size,
            )
    if message.text is not None:
        return ContentType.TEXT, None
    return ContentType.UNKNOWN, None


def _member_events(
    message: TelegramMessage, chat: TelegramChat
) -> list[ConversationMemberEvent]:
    """Reduce a Telegram message to the membership changes it reports.

    An ordinary message reports one thing: its human sender is in this
    conversation. A join or leave service message reports who arrived or
    departed instead, and the person who triggered it is not evidence about
    anyone else, so the sender is left out of those.

    Bots are not people: the bot joining a group says nothing about who reads
    it, and a leave reports the person who is gone, never the one who removed
    them.

    Args:
        message: The raw Telegram message.
        chat: The chat the message belongs to.

    Returns:
        One event per human whose membership changed, in arrival order.
    """
    sender = message.from_user
    is_service_message = bool(message.new_chat_members) or (
        message.left_chat_member is not None
    )
    events: list[ConversationMemberEvent] = []
    if (
        not is_service_message
        and chat.type != "private"
        and sender is not None
        and not sender.is_bot
    ):
        events.append(
            ConversationMemberEvent(
                principal_id=principal_id("telegram", str(sender.id)),
                change=MemberChange.JOINED,
            )
        )
    events.extend(
        ConversationMemberEvent(
            principal_id=principal_id("telegram", str(user.id)),
            change=MemberChange.JOINED,
        )
        for user in message.new_chat_members
        if not user.is_bot
    )
    left = message.left_chat_member
    if left is not None and not left.is_bot:
        events.append(
            ConversationMemberEvent(
                principal_id=principal_id("telegram", str(left.id)),
                change=MemberChange.LEFT,
            )
        )
    return events


def normalize_message(
    update: TelegramUpdate,
    identity: TelegramIdentity,
) -> NormalizedMessage | None:
    """Convert a Telegram update into a normalized message.

    Args:
        update: The parsed Telegram update.
        identity: Chat, admin, and bot identifiers.

    Returns:
        The normalized message, or ``None`` when the update is not an allowed
        message (for example from a different chat).
    """
    message = update.message if update.message is not None else update.edited_message
    if message is None:
        return None
    chat = message.chat

    content_type, attachment = _attachment_for(message)
    text = message.text if message.text is not None else message.caption
    sender = message.from_user
    sender_name = _display_name(sender)
    reply = message.reply_to_message
    bot_username = (identity.bot_username or "").lower()
    mentions_bot = bool(bot_username) and f"@{bot_username}" in (text or "").lower()
    is_reply_to_bot = (
        reply is not None
        and identity.bot_id is not None
        and str(reply.from_user.id if reply.from_user else "") == identity.bot_id
    )
    external_id = f"{chat.id}:{message.message_id}"
    sender_is_admin = (
        identity.admin_user_id is not None
        and str(sender.id if sender else "") == identity.admin_user_id
    )
    return NormalizedMessage(
        id=external_id,
        source=SourceDescriptor(
            id=TELEGRAM_RUNTIME_SOURCE_ID,
            kind="telegram",
            authority=40,
        ),
        conversation_id=str(chat.id),
        principal_id=principal_id("telegram", str(sender.id))
        if sender is not None
        else None,
        sender_is_admin=sender_is_admin,
        sender_authority=95 if sender_is_admin else None,
        timestamp=datetime.fromtimestamp(message.date, tz=UTC),
        content_type=content_type,
        source_message_id=external_id,
        sender_id=pseudonymize(str(sender.id)) if sender else None,
        sender_user_id=str(sender.id) if sender else None,
        sender_name=sender_name,
        text=text,
        reply_to_message_id=str(reply.message_id) if reply else None,
        reply_to_user_id=str(reply.from_user.id)
        if reply is not None and reply.from_user
        else None,
        reply_to_user_name=_display_name(reply.from_user)
        if reply is not None and reply.from_user
        else None,
        mentions_bot=mentions_bot,
        is_reply_to_bot=is_reply_to_bot,
        is_direct_message=chat.type == "private",
        is_sender_allowed=identity.allows_sender(str(sender.id) if sender else None),
        attachments=[attachment] if attachment else [],
        member_events=_member_events(message, chat),
        metadata={"chat_type": chat.type, "update_id": update.update_id},
    )


def normalize_callback(update: TelegramUpdate) -> NormalizedCallback | None:
    """Convert a Telegram callback query into its normalized form.

    Args:
        update: The parsed Telegram update.

    Returns:
        The normalized callback, or ``None`` when the update has no callback.
    """
    query = update.callback_query
    if query is None:
        return None
    message = query.message
    return NormalizedCallback(
        callback_id=query.id,
        data=query.data,
        sender_id=pseudonymize(str(query.from_user.id)),
        sender_chat_id=str(query.from_user.id),
        sender_name=_display_name(query.from_user),
        conversation_id=str(message.chat.id) if message else None,
        message_id=str(message.message_id) if message else None,
    )
