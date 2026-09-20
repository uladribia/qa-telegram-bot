# SPDX-License-Identifier: MIT
"""Domain policies: source authority, reply triggers, and recap scheduling.

Authority values are a policy of the prototype, not probabilities.
"""

from datetime import datetime, timedelta
from enum import IntEnum

from knowledge_bot.domain.enums import SourceType


class Authority(IntEnum):
    """Authority ranking of evidence; a higher value wins."""

    WEB_IN_REVIEW = 30
    TELEGRAM_USER = 40
    WHATSAPP_IMPORT = 50
    WEB_PUBLISHED = 90
    ADMIN_APPROVED = 100


def web_seed_authority(*, in_review: bool) -> Authority:
    """Return the authority of a web Q&A snapshot entry.

    Args:
        in_review: Whether the entry is still marked as in review.

    Returns:
        The matching authority level.
    """
    return Authority.WEB_IN_REVIEW if in_review else Authority.WEB_PUBLISHED


def source_authority(source_type: SourceType) -> Authority:
    """Return the baseline authority for a source type.

    Args:
        source_type: The source being ranked.

    Returns:
        The baseline authority level for that source type.
    """
    match source_type:
        case SourceType.WEB_SEED:
            return Authority.WEB_PUBLISHED
        case SourceType.WHATSAPP_IMPORT:
            return Authority.WHATSAPP_IMPORT
        case SourceType.TELEGRAM:
            return Authority.TELEGRAM_USER
        case SourceType.ADMIN:
            return Authority.ADMIN_APPROVED


def is_ask_command(text: str | None) -> bool:
    """Return whether a message is an explicit ``/ask`` command.

    Args:
        text: The message text, if any.

    Returns:
        ``True`` for ``/ask`` and ``/ask@botname``.
    """
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
    """Return whether a message explicitly asks the bot to answer.

    A bare question is *not* addressed; only explicit signals are.

    Args:
        text: The message text, if any.
        mentions_bot: Whether the bot is mentioned in the message.
        is_reply_to_bot: Whether the message replies to a bot message.
        is_direct_message: Whether the message is a direct message to the bot.

    Returns:
        ``True`` for any explicit addressing signal.
    """
    if is_direct_message or mentions_bot or is_reply_to_bot:
        return True
    return is_ask_command(text)


def is_recap_due(
    last_sent_at: datetime | None,
    now: datetime,
    *,
    interval_hours: int = 24,
) -> bool:
    """Return whether a periodic recap should be sent.

    Args:
        last_sent_at: When the last recap was sent, if ever.
        now: The current time.
        interval_hours: Minimum hours between recaps.

    Returns:
        ``True`` when no recap has been sent or the interval has elapsed.
    """
    if last_sent_at is None:
        return True
    return now - last_sent_at >= timedelta(hours=interval_hours)
