# SPDX-License-Identifier: MIT
"""Reindex the derived vector store from D1 (spec §49).

D1 is the source of truth; Vectorize is rebuildable. This service reads the
indexable records, embeds them, and upserts them with the metadata the
retrieval filters rely on.
"""

from dataclasses import dataclass

from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.index import (
    IndexableMessage,
    IndexableQA,
    SearchIndexSource,
    SearchProjectionRepository,
)
from knowledge_bot.ports.vector_store import VectorRecord, VectorStore

# ponytail: embed/metadata caps keep huge pasted texts (12k+ chars) under the
# free-tier CPU limit and Vectorize metadata size; raise if long documents matter.
_MAX_EMBED_CHARS = 2000
_MAX_METADATA_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ReindexReport:
    """How many records were embedded and upserted, plus the next cursors."""

    qa: int
    messages: int
    next_qa: str | None = None
    next_msg: str | None = None


@dataclass(frozen=True, slots=True)
class ReindexService:
    """Rebuild the vector store from the source of truth."""

    source: SearchIndexSource
    embedder: Embedder
    vectors: VectorStore
    manifest: SearchProjectionRepository
    clock: Clock

    async def reindex(
        self,
        qa_after: str | None = None,
        msg_after: str | None = None,
        limit: int | None = None,
    ) -> ReindexReport:
        """Embed and upsert a batch of indexable records.

        Args:
            qa_after: Cursor into the Q&A versions (id-ordered).
            msg_after: Cursor into the messages (id-ordered).
            limit: Maximum batch size per kind, when chunked.

        Returns:
            Counts and the cursors for the next batch (``None`` when a kind
            is exhausted).
        """
        qa_items = await self.source.list_qa(after=qa_after, limit=limit)
        messages = await self.source.list_messages(after=msg_after, limit=limit)
        records = [
            *await self._qa_records(qa_items),
            *await self._message_records(messages),
        ]
        if records:
            await self.vectors.upsert(records)
            await self.manifest.record(records, self.clock.now())
        next_qa = qa_items[-1].version_id if len(qa_items) == limit else None
        next_msg = messages[-1].message_id if len(messages) == limit else None
        return ReindexReport(
            qa=len(qa_items),
            messages=len(messages),
            next_qa=next_qa,
            next_msg=next_msg,
        )

    async def reindex_qa_version(self, version_id: str) -> bool:
        """Index a single Q&A version (cheap: one embed, one upsert).

        Used after approving a correction so the derived index catches up
        without re-embedding the whole knowledge base.

        Args:
            version_id: The new current version id.

        Returns:
            ``True`` when the version was found and upserted.
        """
        item = await self.source.get_qa(version_id)
        if item is None:
            return False
        records = await self._qa_records([item])
        await self.vectors.upsert(records)
        await self.manifest.record(records, self.clock.now())
        return True

    async def rebuild(self) -> ReindexReport:
        """Delete the known derived projection and rebuild it from SQL."""
        vector_ids = await self.manifest.list_vector_ids()
        for start in range(0, len(vector_ids), 100):
            await self.vectors.delete(vector_ids[start : start + 100])
        await self.manifest.clear()
        return await self.reindex()

    async def _qa_records(self, items: list[IndexableQA]) -> list[VectorRecord]:
        if not items:
            return []
        texts = [f"{item.question}\n{item.answer}" for item in items]
        embeddings = await self.embedder.embed(texts)
        return [
            VectorRecord(
                id=f"qa:{item.qa_item_id}",
                values=vector,
                metadata={
                    "kind": "qa",
                    "object_id": item.qa_item_id,
                    "version_id": item.version_id,
                    "status": "active",
                    "authority": item.authority,
                    "scope_key": item.scope_key,
                    "canonical_key": item.canonical_key,
                    "question": item.question,
                    "text": item.answer,
                    "source_anchor": item.source_anchor,
                    "url": item.url,
                    "date": item.date,
                    "author": item.author,
                },
            )
            for item, vector in zip(items, embeddings, strict=True)
        ]

    async def _message_records(
        self, messages: list[IndexableMessage]
    ) -> list[VectorRecord]:
        if not messages:
            return []
        # A reply matched to its parent question is embedded together, so the
        # pair retrieves as one unit instead of an orphaned answer.
        texts = [
            f"{message.question}\n{message.text}" if message.question else message.text
            for message in messages
        ]
        embeddings = await self.embedder.embed(
            [text[:_MAX_EMBED_CHARS] for text in texts]
        )
        return [
            VectorRecord(
                id=f"msg:{message.message_id}",
                values=vector,
                metadata={
                    "kind": "message_evidence",
                    "object_id": message.message_id,
                    "source_kind": message.source_kind,
                    "authority": message.authority,
                    "scope_key": message.scope_key,
                    "text": message.text[:_MAX_METADATA_CHARS],
                    "question": message.question,
                    "author": message.author,
                    "date": message.date,
                },
            )
            for message, vector in zip(messages, embeddings, strict=True)
        ]
