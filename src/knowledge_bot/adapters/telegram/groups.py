# SPDX-License-Identifier: MIT
"""Registration of a served Telegram group.

Binding a chat to a logical space is Telegram-specific: the channel, the source
kind, and the source authority are all declared by this connector.
"""

from knowledge_bot.adapters.telegram.identity import TELEGRAM_RUNTIME_SOURCE_ID
from knowledge_bot.infrastructure.context import AppContext


async def bind_telegram_group(
    context: AppContext, chat_id: str, title: str | None, space_id: str | None
) -> str:
    """Bind a served Telegram group to a logical space, idempotently.

    Args:
        context: The application context.
        chat_id: The raw Telegram chat id.
        title: The group title, when known.
        space_id: The space to bind, or ``None`` to resolve by chat id.

    Returns:
        The logical space id the group is now served under.
    """
    return await context.spaces.bind(
        channel="telegram",
        external_conversation_id=chat_id,
        conversation_id=chat_id,
        space_id=space_id,
        title=title,
        source_id=TELEGRAM_RUNTIME_SOURCE_ID,
        source_kind="telegram",
        source_authority=40,
    )
