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


@dataclass(frozen=True, slots=True)
class ReindexReport:
    """How many records were embedded and upserted."""

    qa: int
    messages: int


@dataclass(frozen=True, slots=True)
class ReindexService:
    """Rebuild the vector store from the source of truth."""

    source: SearchIndexSource
    embedder: Embedder
    vectors: VectorStore

    async def reindex(self) -> ReindexReport:
        """Embed and upsert every indexable record.

        Returns:
            The number of Q&A versions and messages indexed.
        """
        qa_items = await self.source.list_qa()
        messages = await self.source.list_messages()
        records = [
            *await self._qa_records(qa_items),
            *await self._message_records(messages),
        ]
        await self.vectors.upsert(records)
        return ReindexReport(qa=len(qa_items), messages=len(messages))

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
                    "question": item.question,
                    "text": item.answer,
                },
            )
            for item, vector in zip(items, embeddings, strict=False)
        ]

    async def _message_records(
        self, messages: list[IndexableMessage]
    ) -> list[VectorRecord]:
        if not messages:
            return []
        embeddings = await self.embedder.embed([message.text for message in messages])
        return [
            VectorRecord(
                id=message.message_id,
                values=vector,
                metadata={
                    "kind": "message",
                    "object_id": message.message_id,
                    "source_type": message.source_type,
                    "authority": message.authority,
                    "text": message.text,
                },
            )
            for message, vector in zip(messages, embeddings, strict=False)
        ]
