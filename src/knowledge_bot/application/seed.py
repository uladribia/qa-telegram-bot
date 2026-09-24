# SPDX-License-Identifier: MIT
"""Seed the knowledge base from a web snapshot and imports (spec §7, §8).

Seeding is idempotent: an existing canonical key is skipped, and messages use the
ingest idempotency key.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.entities import QAItem, QAVersion, Source
from knowledge_bot.domain.enums import QAStatus
from knowledge_bot.domain.identity import source_instance_id
from knowledge_bot.domain.scope import GLOBAL_SCOPE, Scope, is_global
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import (
    QAItemRepository,
    QAVersionRepository,
    SourceRepository,
)


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
    qa_renewed: int
    messages: int


@dataclass(frozen=True, slots=True)
class SeedService:
    """Persist seed Q&A and imported messages."""

    qa_items: QAItemRepository
    qa_versions: QAVersionRepository
    sources: SourceRepository
    ingestor: MessageIngestor
    clock: Clock

    async def _ensure_source(self, entry: SeedQA) -> None:
        """Create the source instance declared by the seed connector."""
        source_id = source_instance_id(entry.source_kind, entry.source_url)
        existing = await self.sources.get(source_id)
        if existing is not None:
            if existing.canonical_url != entry.source_url:
                await self.sources.save(
                    replace(existing, canonical_url=entry.source_url)
                )
            return
        await self.sources.add(
            Source(
                id=source_id,
                source_type=entry.source_kind,
                authority=entry.source_authority,
                created_at=self.clock.now(),
                title=entry.source_kind,
                canonical_url=entry.source_url,
            )
        )

    async def seed_qa(
        self,
        entries: list[SeedQA],
        scope: Scope = GLOBAL_SCOPE,
        renew: bool = False,
    ) -> tuple[int, int, int, list[str]]:
        """Persist a batch of seed Q&A entries.

        Without ``renew``, existing entries are skipped (idempotent import).
        With ``renew``, an entry whose answer differs from the current version
        creates a new version on the existing item and makes it current, so
        the most recent update always prevails. Old versions are kept.

        Args:
            entries: The parsed snapshot entries.
            scope: The knowledge scope the entries belong to.
            renew: Update changed entries instead of skipping them.

        Returns:
            A tuple of (created, skipped, renewed, version_ids), where
            ``version_ids`` are the current versions that need indexing: the
            newly created and the renewed ones.
        """
        created = 0
        skipped = 0
        renewed = 0
        version_ids: list[str] = []
        now = self.clock.now()
        for entry in entries:
            await self._ensure_source(entry)
            key = entry.source_anchor or stable_id(entry.question)
            existing = await self.qa_items.get_by_canonical_key(key, scope)
            if existing is not None:
                if not renew:
                    skipped += 1
                    continue
                if await self._renew_item(existing, entry, now):
                    item = await self.qa_items.get_by_canonical_key(key, scope)
                    assert item is not None and item.current_version_id
                    version_ids.append(item.current_version_id)
                    renewed += 1
                else:
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
                    authority=min(entry.source_authority, 30)
                    if in_review
                    else entry.source_authority,
                    origin=entry.source_kind,
                    created_at=entry.retrieved_at,
                    source_url=_anchored(entry.source_url, entry.source_anchor),
                )
            )
            version_ids.append(version_id)
            created += 1
        return created, skipped, renewed, version_ids

    async def _renew_item(self, item: QAItem, entry: SeedQA, now: datetime) -> bool:
        """Add a newer web version to an existing item when the answer changed.

        Args:
            item: The existing Q&A item.
            entry: The freshly snapshotted entry.
            now: The renewal timestamp.

        Returns:
            ``True`` when a new version was created.
        """
        current = (
            await self.qa_versions.get(item.current_version_id)
            if item.current_version_id
            else None
        )
        if current is not None and current.answer == entry.answer:
            return False
        in_review = entry.status == "in_review"
        version = QAVersion(
            id=f"qav:{item.id}:{int(now.timestamp())}",
            qa_id=item.id,
            answer=entry.answer,
            authority=min(entry.source_authority, 30)
            if in_review
            else entry.source_authority,
            origin=entry.source_kind,
            created_at=now,
            supersedes_version_id=item.current_version_id,
            source_url=_anchored(entry.source_url, entry.source_anchor),
        )
        await self.qa_versions.add(version)
        await self.qa_items.save(
            replace(item, updated_at=now, current_version_id=version.id)
        )
        return True

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
