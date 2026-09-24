# SPDX-License-Identifier: MIT
"""Pure authority and intake policies."""

from datetime import datetime, timedelta
from enum import IntEnum


class Authority(IntEnum):
    """Authority ranking of evidence; a higher value wins."""

    WEB_IN_REVIEW = 30
    TELEGRAM_USER = 40
    WHATSAPP_IMPORT = 50
    WEB_PUBLISHED = 90
    ADMIN_APPROVED = 100


def effective_message_authority(
    source_authority: int, sender_authority: int | None
) -> int:
    """Return the effective authority of one message evidence record."""
    return max(source_authority, sender_authority or 0)


def is_ask_command(text: str | None) -> bool:
    """Return whether text contains an explicit ask command."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    token = stripped.split(maxsplit=1)[0].lower()
    return token == "/ask" or token.startswith("/ask@")


def is_addressed(
    text: str | None,
    *,
    mentions_bot: bool = False,
    is_reply_to_bot: bool = False,
    is_direct_message: bool = False,
) -> bool:
    """Return whether a message explicitly addresses the bot."""
    if is_direct_message or mentions_bot or is_reply_to_bot:
        return True
    return is_ask_command(text)


def is_recap_due(
    last_sent_at: datetime | None, now: datetime, *, interval_hours: int = 24
) -> bool:
    """Return whether a periodic interval has elapsed."""
    if last_sent_at is None:
        return True
    return now - last_sent_at >= timedelta(hours=interval_hours)
