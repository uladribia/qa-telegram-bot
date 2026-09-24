# SPDX-License-Identifier: MIT
"""Bounded rebuild orchestration for the derived search projection."""

from dataclasses import dataclass

from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.domain.errors import ModelUnavailableError, ProjectionError
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.index import SearchIndexSource


@dataclass(frozen=True, slots=True)
class ReindexReport:
    """How many records were projected and the next cursors."""

    qa: int
    messages: int
    next_qa: str | None = None
    next_msg: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionCleanupReport:
    """How many known and legacy vector ids were removed."""

    removed: int = 0


@dataclass(frozen=True, slots=True)
class ProjectionAttempt:
    """Result of attempting a projection without changing SQL truth."""

    status: str


@dataclass(frozen=True, slots=True)
class ReindexService:
    """Delegate all vector writes to the shared projection service."""

    source: SearchIndexSource
    projector: SearchProjectionService
    clock: Clock

    async def reindex(
        self,
        qa_after: str | None = None,
        msg_after: str | None = None,
        limit: int = 50,
    ) -> ReindexReport:
        """Project one bounded batch of current SQL records."""
        qa_items = await self.source.list_qa(after=qa_after, limit=limit)
        messages = await self.source.list_messages(after=msg_after, limit=limit)
        for item in qa_items:
            await self.projector.project_qa(item)
        for message in messages:
            await self.projector.project_message(message)
        return ReindexReport(
            len(qa_items),
            len(messages),
            qa_items[-1].version_id if len(qa_items) == limit else None,
            messages[-1].message_id if len(messages) == limit else None,
        )

    async def cleanup_projection(self) -> ProjectionCleanupReport:
        """Delete known derived vectors without making an AI call."""
        vector_ids = list(
            dict.fromkeys(
                [
                    *await self.projector.manifest.list_vector_ids(),
                    *await self.source.list_legacy_vector_ids(),
                ]
            )
        )
        await self.projector.remove(vector_ids)
        await self.projector.manifest.clear()
        return ProjectionCleanupReport(len(vector_ids))

    async def reindex_qa_version(self, version_id: str) -> bool:
        """Project one version only if it is current."""
        item = await self.source.get_qa(version_id)
        if item is None:
            return False
        await self.projector.project_qa(item)
        return True

    async def try_reindex_qa_version(self, version_id: str) -> ProjectionAttempt:
        """Attempt one current Q&A projection and preserve SQL approval."""
        item = await self.source.get_qa(version_id)
        if item is None:
            return ProjectionAttempt("not_current")
        try:
            await self.projector.project_qa(item)
        except (ProjectionError, ModelUnavailableError, RuntimeError, ValueError):
            return ProjectionAttempt("failed")
        return ProjectionAttempt("indexed")
