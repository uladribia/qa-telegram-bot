# SPDX-License-Identifier: MIT
"""In-memory correction transaction store for integration tests."""

from copy import deepcopy

from knowledge_bot.domain.entities import Feedback, QAVersion
from knowledge_bot.ports.transactions import ApproveCorrectionCommand
from tests.fakes.repositories import (
    InMemoryFeedbackRepository,
    InMemoryQAEvidenceRepository,
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
)


class InMemoryCorrectionCommitStore:
    """Write the same semantic correction records as the production store."""

    def __init__(
        self,
        items: InMemoryQAItemRepository,
        versions: InMemoryQAVersionRepository,
        evidence: InMemoryQAEvidenceRepository,
        feedback: InMemoryFeedbackRepository,
    ) -> None:
        """Wire the shared repositories used by one atomic operation."""
        self.items = items
        self.versions = versions
        self.evidence = evidence
        self.feedback = feedback

    async def approve(self, command: ApproveCorrectionCommand) -> QAVersion:
        """Persist all correction writes or none of them."""
        item_state = deepcopy(self.items._items)
        version_state = deepcopy(self.versions._items)
        evidence_state = deepcopy(self.evidence._items)
        feedback_state = deepcopy(self.feedback._items)
        try:
            await self.versions.add(command.version)
            if await self.items.get(command.item.id) is None:
                await self.items.add(command.item)
            else:
                await self.items.save(command.item)
            await self.evidence.add(command.evidence)
            await self.feedback.save(command.feedback)
        except Exception:
            self.items._items = item_state
            self.versions._items = version_state
            self.evidence._items = evidence_state
            self.feedback._items = feedback_state
            raise
        return command.version

    async def reject(self, feedback: Feedback) -> Feedback:
        """Persist one rejection decision."""
        await self.feedback.save(feedback)
        return feedback
