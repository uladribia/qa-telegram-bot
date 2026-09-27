# SPDX-License-Identifier: MIT
"""Promote a seeded Q&A item from ``under_review`` into the live base.

Seeding a snapshot entry with ``status: in_review`` stores it as
``under_review``. That is a write-only state: retrieval filters on
``status: active``, nothing embeds the item, and no feedback record exists to
approve through the reviewer flow, so a seeded item can never reach the base on
its own. This service is the missing half — an operator publishes it explicitly,
which is also the only way an unreviewed snapshot ever becomes answerable.

The reviewer flow does not need it: ``FeedbackService.approve`` already writes
the item back as ``active`` in the same transaction as the new version. This
exists for content that arrives by seed rather than by correction.

Only the status moves. No answer text is edited, no version is created, and the
previous status is not recorded anywhere, so promotion is not reversible by
design: withdrawing a published entry is a different operation, and pretending
otherwise would hide a real content decision behind a flag.
"""

from dataclasses import dataclass, replace

from knowledge_bot.domain.entities import QAItem
from knowledge_bot.domain.enums import QAStatus
from knowledge_bot.domain.identity import canonical_key_for
from knowledge_bot.domain.scope import GLOBAL_SCOPE
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import QAItemRepository, QAVersionRepository


@dataclass(frozen=True, slots=True)
class QAPromoter:
    """Publish a ``under_review`` Q&A item so retrieval can see it."""

    qa_items: QAItemRepository
    qa_versions: QAVersionRepository
    clock: Clock

    async def promote(self, target: str) -> QAItem | None:
        """Set the item's status to active.

        Args:
            target: A Q&A item id, or the canonical question text as the
                operator sees it in the review report.

        Returns:
            The promoted item, or ``None`` when the target matches no item. An
            item that is already active is returned unchanged, so the command
            is idempotent.
        """
        item = await self._resolve(target)
        if item is None:
            return None
        if item.status is QAStatus.ACTIVE:
            return item
        promoted = replace(item, status=QAStatus.ACTIVE, updated_at=self.clock.now())
        await self.qa_items.save(promoted)
        return promoted

    async def _resolve(self, target: str) -> QAItem | None:
        """Find the item an operator target refers to."""
        text = target.strip()
        if not text:
            return None
        if text.startswith("qa-"):
            return await self.qa_items.get(text)
        return await self.qa_items.get_by_canonical_key(
            canonical_key_for(text), GLOBAL_SCOPE
        )
