# SPDX-License-Identifier: MIT
"""In-memory correction transaction store for integration tests."""

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
        """Persist the complete correction decision."""
        await self.versions.add(command.version)
        await self.items.save(command.item)
        await self.evidence.add(command.evidence)
        await self.feedback.save(command.feedback)
        return command.version

    async def reject(self, feedback: Feedback) -> Feedback:
        """Persist one rejection decision."""
        await self.feedback.save(feedback)
        return feedback
