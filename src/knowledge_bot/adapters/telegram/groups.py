# SPDX-License-Identifier: MIT
"""Registration of a served Telegram group.

Binding a chat to a logical space is Telegram-specific: the channel, the source
kind, and the source authority are all declared by this connector.
"""

from knowledge_bot.adapters.telegram.identity import TELEGRAM_RUNTIME_SOURCE_ID
from knowledge_bot.domain.enums import BotMode
from knowledge_bot.infrastructure.context import AppContext


async def bind_telegram_group(
    context: AppContext,
    chat_id: str,
    title: str | None,
    space_id: str | None,
    bot_mode: BotMode | None = None,
) -> str:
    """Bind a served Telegram group to a logical space, idempotently.

    Args:
        context: The application context.
        chat_id: The raw Telegram chat id.
        title: The group title, or ``None`` to keep the registered one.
        space_id: The space to bind, or ``None`` to resolve by chat id.
        bot_mode: How the bot behaves there, or ``None`` to keep the current one.

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
        bot_mode=bot_mode,
    )
