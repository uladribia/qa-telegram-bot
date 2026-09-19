# SPDX-License-Identifier: MIT
"""Seed the knowledge base from a web snapshot and imports (spec §7, §8).

Seeding is idempotent: an existing canonical key is skipped, and messages use the
ingest idempotency key.
"""

import hashlib
from dataclasses import dataclass

from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.contracts.seed import SeedQA
from knowledge_bot.domain.entities import QAItem, QAVersion
from knowledge_bot.domain.enums import QAOrigin, QAStatus
from knowledge_bot.domain.policies import web_seed_authority
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.repositories import QAItemRepository, QAVersionRepository


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
    ingestor: MessageIngestor
    clock: Clock

    async def seed_qa(self, entries: list[SeedQA]) -> tuple[int, int]:
        """Persist a batch of seed Q&A entries.

        Args:
            entries: The parsed snapshot entries.

        Returns:
            A tuple of (created, skipped).
        """
        created = 0
        skipped = 0
        now = self.clock.now()
        for entry in entries:
            key = entry.source_anchor or stable_id(entry.question)
            if await self.qa_items.get_by_canonical_key(key) is not None:
                skipped += 1
                continue
            in_review = entry.status == "in_review"
            qa_id = f"qa-{stable_id(key)}"
            version_id = f"qav-{stable_id(key)}-1"
            status = QAStatus.UNDER_REVIEW if in_review else QAStatus.ACTIVE
            await self.qa_items.add(
                QAItem(
                    id=qa_id,
                    canonical_key=key,
                    canonical_question=entry.question,
                    status=status,
                    created_at=now,
                    updated_at=now,
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
                )
            )
            created += 1
        return created, skipped

    async def seed_messages(self, messages: list[NormalizedMessage]) -> int:
        """Ingest a batch of imported messages.

        Args:
            messages: The normalized messages to ingest.

        Returns:
            The number of newly created messages.
        """
        created = 0
        for message in messages:
            result = await self.ingestor.ingest(message)
            if result.created:
                created += 1
        return created
