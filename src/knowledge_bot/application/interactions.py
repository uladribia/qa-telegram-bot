# SPDX-License-Identifier: MIT
"""Application service for durable reply interactions."""

from dataclasses import dataclass
from datetime import datetime

from knowledge_bot.domain.entities import TelegramInteraction
from knowledge_bot.ports.repositories import TelegramInteractionRepository


@dataclass(frozen=True, slots=True)
class InteractionService:
    """Expose durable interaction operations without leaking a repository."""

    repository: TelegramInteractionRepository

    async def get(self, external_message_id: str) -> TelegramInteraction | None:
        """Return one interaction by external prompt id."""
        return await self.repository.get(external_message_id)

    async def consume(
        self, external_message_id: str, consumed_at: datetime
    ) -> TelegramInteraction | None:
        """Atomically consume one unused interaction."""
        return await self.repository.consume(external_message_id, consumed_at)

    async def add(self, interaction: TelegramInteraction) -> None:
        """Persist one interaction."""
        await self.repository.add(interaction)
