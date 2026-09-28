# SPDX-License-Identifier: MIT
"""A search-index source that reads the Q&A the repositories actually hold.

``FakeSearchIndexSource`` carries records a test hands it, which is enough for
a projection that already knows what it is projecting. It cannot express the
flow that matters most for the correction paths: approve a correction, let the
application ask the index for the version it just wrote, and project it. There
the version id only exists after the write.

This source closes that gap the way ``D1SearchIndexSource`` closes it in
production: it joins the Q&A items and versions the application stored, and
returns a record only for an active item's current version.
"""

from datetime import datetime

from knowledge_bot.domain.enums import QAStatus
from knowledge_bot.ports.index import IndexableMessage, IndexableQA
from tests.fakes.repositories import (
    InMemoryQAItemRepository,
    InMemoryQAVersionRepository,
)


def _date_part(value: datetime) -> str:
    """Return a DD/MM/YYYY date, the shape the citations render."""
    return value.strftime("%d/%m/%Y")


def _exact_url(base: object, anchor: object) -> str | None:
    """Compose the anchor-specific URL for a web Q&A, when possible.

    Mirrors the production rule: a correction stores no URL, and its anchor is
    an opaque canonical key, so only a snapshot URL gains the fragment.
    """
    base_text = base if isinstance(base, str) and base else None
    anchor_text = anchor if isinstance(anchor, str) and anchor else None
    if not base_text:
        return None
    if anchor_text and base_text.endswith("/"):
        return f"{base_text}#{anchor_text}"
    return base_text


class RepositorySearchIndexSource:
    """A ``SearchIndexSource`` over the in-memory Q&A repositories.

    Messages are not covered: the correction and retrieval flows under test
    project Q&A only, and a message projection would need the message
    repository, which none of them reads through this source.
    """

    def __init__(
        self,
        items: InMemoryQAItemRepository,
        versions: InMemoryQAVersionRepository,
    ) -> None:
        """Create a source over the given repositories.

        Args:
            items: The repository holding Q&A items.
            versions: The repository holding Q&A versions.
        """
        self._items = items
        self._versions = versions

    async def _record(self, item_id: str) -> IndexableQA | None:
        """Return the current active record for an item, if it has one."""
        item = await self._items.get(item_id)
        if item is None or item.status is not QAStatus.ACTIVE:
            return None
        if item.current_version_id is None:
            return None
        version = await self._versions.get(item.current_version_id)
        if version is None:
            return None
        return IndexableQA(
            qa_item_id=item.id,
            version_id=version.id,
            question=item.canonical_question,
            answer=version.answer,
            authority=version.authority,
            canonical_key=item.canonical_key,
            source_anchor=version.source_anchor,
            url=_exact_url(version.source_url, version.source_anchor),
            date=_date_part(version.created_at),
            author=version.author,
            scope_key=item.scope_key,
        )

    async def get_qa(self, version_id: str) -> IndexableQA | None:
        """Return one current Q&A version, by version id."""
        version = await self._versions.get(version_id)
        return None if version is None else await self._record(version.qa_id)

    async def get_current_qa_by_item_id(self, qa_item_id: str) -> IndexableQA | None:
        """Return the current active Q&A record for an item."""
        return await self._record(qa_item_id)

    async def get_indexable_message(self, message_id: str) -> IndexableMessage | None:
        """Return no message: this source covers Q&A only."""
        del message_id
        return None

    async def list_qa(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableQA]:
        """Return the active Q&A versions to index, in version-id order."""
        records: list[IndexableQA] = []
        for item in await self._items.all():
            record = await self._record(item.id)
            if record is not None and (after is None or record.version_id > after):
                records.append(record)
        records.sort(key=lambda record: record.version_id)
        return records if limit is None else records[:limit]

    async def list_messages(
        self, after: str | None = None, limit: int | None = None
    ) -> list[IndexableMessage]:
        """Return no messages: this source covers Q&A only."""
        del after, limit
        return []

    async def list_legacy_vector_ids(self) -> list[str]:
        """Return no legacy ids: a rebuild starts from the manifest."""
        return []
