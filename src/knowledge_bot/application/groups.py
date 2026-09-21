# SPDX-License-Identifier: MIT
"""Register Telegram groups the bot serves.

Registration is idempotent: re-adding a known group refreshes its title and
changes nothing else. A registered group is a conversation row under the
Telegram runtime source; messages, Q&A, and sources can then be scoped to it.
"""

from dataclasses import dataclass, replace

from knowledge_bot.domain.entities import Conversation, Source
from knowledge_bot.domain.enums import SourceType
from knowledge_bot.domain.policies import source_authority
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    SourceRepository,
)

TELEGRAM_SOURCE_ID = SourceType.TELEGRAM.value


@dataclass(frozen=True, slots=True)
class GroupRegistrar:
    """Register a Telegram group as a scopeable conversation."""

    sources: SourceRepository
    conversations: ConversationRepository
    clock: Clock

    async def register(self, chat_id: str, title: str | None = None) -> bool:
        """Register a group, or refresh its title.

        Args:
            chat_id: The Telegram chat id of the group.
            title: A human-readable name for the group.

        Returns:
            ``True`` when the group was newly created.
        """
        if not chat_id.strip():
            message = "Group chat id must not be empty"
            raise ValueError(message)
        if await self.sources.get(TELEGRAM_SOURCE_ID) is None:
            await self.sources.add(
                Source(
                    id=TELEGRAM_SOURCE_ID,
                    source_type=SourceType.TELEGRAM,
                    authority=int(source_authority(SourceType.TELEGRAM)),
                    created_at=self.clock.now(),
                    title=TELEGRAM_SOURCE_ID,
                    is_mutable=True,
                )
            )
        existing = await self.conversations.get(chat_id)
        if existing is not None:
            if existing.title != title:
                await self.conversations.save(replace(existing, title=title))
            return False
        await self.conversations.add(
            Conversation(
                id=chat_id,
                source_id=TELEGRAM_SOURCE_ID,
                created_at=self.clock.now(),
                external_id=chat_id,
                title=title,
            )
        )
        return True
