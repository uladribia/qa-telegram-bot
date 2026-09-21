# SPDX-License-Identifier: MIT
"""Revert an approved correction to the version it superseded.

This is the only way to roll a correction back: an admin CLI operation, never
a Telegram action. Nothing is deleted — the reverted version stays in the
history, only the ``current_version_id`` pointer moves back.
"""

from dataclasses import dataclass, replace

from knowledge_bot.domain.entities import QAVersion
from knowledge_bot.ports.repositories import QAItemRepository, QAVersionRepository


@dataclass(frozen=True, slots=True)
class CorrectionReverter:
    """Move a Q&A item's current version back one step."""

    qa_items: QAItemRepository
    qa_versions: QAVersionRepository

    async def revert(self, qa_item_id: str) -> QAVersion | None:
        """Restore the version the current one superseded, if any.

        Args:
            qa_item_id: The Q&A item to revert.

        Returns:
            The restored version, or ``None`` when the item does not exist or
            has no superseded version to restore.
        """
        item = await self.qa_items.get(qa_item_id)
        if item is None or item.current_version_id is None:
            return None
        current = await self.qa_versions.get(item.current_version_id)
        if current is None or current.supersedes_version_id is None:
            return None
        restored = await self.qa_versions.get(current.supersedes_version_id)
        if restored is None:
            return None
        await self.qa_items.save(replace(item, current_version_id=restored.id))
        return restored
