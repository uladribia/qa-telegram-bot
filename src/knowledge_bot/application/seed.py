# SPDX-License-Identifier: MIT
"""Seed the knowledge base from a web snapshot and imports (spec §7, §8).

Seeding is idempotent: an existing canonical key is skipped, and messages use the
ingest idempotency key.
"""

import hashlib
from dataclasses import dataclass, replace

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.entities import QAItem, QAVersion, Source
from knowledge_bot.domain.enums import QAOrigin, QAStatus, SourceType
from knowledge_bot.domain.policies import source_authority, web_seed_authority
from knowledge_bot.domain.scope import GLOBAL_SCOPE, Scope, is_global
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    QAItemRepository,
    QAVersionRepository,
    SourceRepository,
)

WEB_SEED_SOURCE_ID = SourceType.WEB_SEED.value


def _anchored(base: str | None, anchor: str | None) -> str | None:
    """Return the exact anchored URL for a snapshot entry, if any.

    Args:
        base: The snapshot's base URL.
        anchor: The entry's anchor.

    Returns:
        ``base#anchor`` when both are present, otherwise whichever exists.
    """
    if base and anchor:
        return f"{base}#{anchor}"
    return base or anchor


def stable_id(value: str) -> str:
    """Return a short deterministic id for a value.

    Args:
        value: The value to hash.

    Returns:
        A short hexadecimal hash.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SeedReport:
    """How many records were created."""

    qa: int
    qa_skipped: int
    messages: int


@dataclass(frozen=True, slots=True)
class SeedService:
    """Persist seed Q&A and imported messages."""

    qa_items: QAItemRepository
    qa_versions: QAVersionRepository
    sources: SourceRepository
    ingestor: MessageIngestor
    clock: Clock

    async def _ensure_web_seed_source(self, canonical_url: str) -> None:
        """Create the web-seed source row that citations link to."""
        existing = await self.sources.get(WEB_SEED_SOURCE_ID)
        if existing is not None:
            if existing.canonical_url != canonical_url:
                await self.sources.save(replace(existing, canonical_url=canonical_url))
            return
        await self.sources.add(
            Source(
                id=WEB_SEED_SOURCE_ID,
                source_type=SourceType.WEB_SEED,
                authority=int(source_authority(SourceType.WEB_SEED)),
                created_at=self.clock.now(),
                title="Web Q&A",
                canonical_url=canonical_url,
            )
        )

    async def seed_qa(
        self,
        entries: list[SeedQA],
        scope: Scope = GLOBAL_SCOPE,
    ) -> tuple[int, int]:
        """Persist a batch of seed Q&A entries.

        Args:
            entries: The parsed snapshot entries.
            scope: The knowledge scope the entries belong to.

        Returns:
            A tuple of (created, skipped).
        """
        created = 0
        skipped = 0
        now = self.clock.now()
        for entry in entries:
            await self._ensure_web_seed_source(entry.source_url)
            key = entry.source_anchor or stable_id(entry.question)
            if await self.qa_items.get_by_canonical_key(key, scope) is not None:
                skipped += 1
                continue
            in_review = entry.status == "in_review"
            suffix = "" if is_global(scope) else f":{scope}"
            qa_id = f"qa-{stable_id(key)}{suffix}"
            version_id = f"qav-{stable_id(key)}-1{suffix}"
            status = QAStatus.UNDER_REVIEW if in_review else QAStatus.ACTIVE
            await self.qa_items.add(
                QAItem(
                    id=qa_id,
                    canonical_key=key,
                    canonical_question=entry.question,
                    status=status,
                    created_at=now,
                    updated_at=now,
                    scope=scope,
                    current_version_id=version_id,
                )
            )
            await self.qa_versions.add(
                QAVersion(
                    id=version_id,
                    qa_id=qa_id,
                    answer=entry.answer,
                    authority=int(web_seed_authority(in_review=in_review)),
                    origin=QAOrigin.WEB_SEED,
                    created_at=entry.retrieved_at,
                    source_url=_anchored(entry.source_url, entry.source_anchor),
                )
            )
            created += 1
        return created, skipped

    async def seed_messages(
        self,
        messages: list[NormalizedMessage],
        scope: Scope = GLOBAL_SCOPE,
    ) -> int:
        """Ingest a batch of imported messages.

        Messages are inherently scoped by their own conversation; ``scope``
        only tags the import source.

        Args:
            messages: The normalized messages to ingest.
            scope: The knowledge scope of the import source.

        Returns:
            The number of newly created messages.
        """
        created = 0
        for message in messages:
            result = await self.ingestor.ingest(message, source_scope=scope)
            if result.created:
                created += 1
        return created
