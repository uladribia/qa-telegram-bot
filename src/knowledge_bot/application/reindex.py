# SPDX-License-Identifier: MIT
"""Reindex the derived vector store from D1 (spec §49).

D1 is the source of truth; Vectorize is rebuildable. This service reads the
indexable records, embeds them, and upserts them with the metadata the
retrieval filters rely on.
"""

from dataclasses import dataclass

from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.index import IndexableMessage, IndexableQA, SearchIndexSource
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
        return True

    async def _qa_records(self, items: list[IndexableQA]) -> list[VectorRecord]:
        if not items:
            return []
        texts = [f"{item.question}\n{item.answer}" for item in items]
        embeddings = await self.embedder.embed(texts)
        return [
            VectorRecord(
                id=item.version_id,
                values=vector,
                metadata={
                    "kind": "qa_version",
                    "object_id": item.version_id,
                    "status": "active",
                    "authority": item.authority,
                    "scope": item.scope,
                    "question": item.question,
                    "text": item.answer,
                    "anchor": item.anchor,
                    "url": item.url,
                    "date": item.date,
                    "author": item.author,
                },
            )
            for item, vector in zip(items, embeddings, strict=False)
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
                id=message.message_id,
                values=vector,
                metadata={
                    "kind": "message",
                    "object_id": message.message_id,
                    "source_type": message.source_type,
                    "authority": message.authority,
                    "scope": message.conversation_id,
                    "text": message.text[:_MAX_METADATA_CHARS],
                    "question": message.question,
                    "author": message.author,
                    "date": message.date,
                },
            )
            for message, vector in zip(messages, embeddings, strict=False)
        ]
