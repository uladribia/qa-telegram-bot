# SPDX-License-Identifier: MIT
"""Durable ordering and repair for derived search projections."""

from dataclasses import dataclass

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.domain.enums import AiWorkClass, ProjectionState
from knowledge_bot.domain.errors import ModelUnavailableError, ProjectionError
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.index import (
    IndexableMessage,
    IndexableQA,
    SearchIndexSource,
    SearchProjectionRepository,
)
from knowledge_bot.ports.vector_store import VectorRecord, VectorStore


@dataclass(frozen=True, slots=True)
class ProjectionRepairReport:
    """Counts returned by one bounded repair operation."""

    repaired: int = 0
    removed: int = 0
    failed: int = 0
    stopped_by_budget: bool = False


@dataclass(frozen=True, slots=True)
class SearchProjectionService:
    """Own the only vector and manifest ordering used by the app."""

    source: SearchIndexSource
    embedder: Embedder
    vectors: VectorStore
    manifest: SearchProjectionRepository
    clock: Clock
    budget: AiBudget | None = None

    async def project_qa(self, item: IndexableQA) -> None:
        """Replace a stable Q&A vector without retaining stale truth."""
        vector_id = f"qa:{item.qa_item_id}"
        await self.manifest.reserve(
            vector_id, "qa", item.qa_item_id, item.version_id, self.clock.now()
        )
        try:
            await self.vectors.delete([vector_id])
            # Question-focused embedding: the vector text is the question only;
            # the answer stays in metadata.
            values = (await self.embedder.embed([item.question]))[0]
            metadata: dict[str, object] = {
                "kind": "qa",
                "object_id": item.qa_item_id,
                "version_id": item.version_id,
                "status": "active",
                "scope_key": item.scope_key,
                "canonical_key": item.canonical_key,
                "authority": item.authority,
                "question": item.question,
                "text": item.answer,
                "source_anchor": item.source_anchor,
                "url": item.url,
                "date": item.date,
                "author": item.author,
            }
            await self.vectors.upsert(
                [VectorRecord(id=vector_id, values=values, metadata=metadata)]
            )
            await self.manifest.mark_active(
                vector_id, item.version_id, self.clock.now()
            )
        except ModelUnavailableError:
            await self.manifest.mark_failed(
                vector_id, item.version_id, "model_unavailable", self.clock.now()
            )
            raise
        except (RuntimeError, ValueError):
            await self.manifest.mark_failed(
                vector_id, item.version_id, "vector_write_failed", self.clock.now()
            )
            raise ProjectionError("vector_write_failed") from None

    async def project_message(
        self,
        message: IndexableMessage,
        precomputed_embedding: tuple[float, ...] | None = None,
    ) -> None:
        """Replace one ordinary message-evidence vector."""
        vector_id = f"msg:{message.message_id}"
        await self.manifest.reserve(
            vector_id, "message", message.message_id, None, self.clock.now()
        )
        try:
            await self.vectors.delete([vector_id])
            # Question-focused embeddings: paired evidence embeds the context
            # question only; a standalone factual update embeds its own text.
            values = list(precomputed_embedding or [])
            if not values:
                text = message.question or message.text
                values = (await self.embedder.embed([text]))[0]
            metadata: dict[str, object] = {
                "kind": "message_evidence",
                "object_id": message.message_id,
                "scope_key": message.scope_key,
                "authority": message.authority,
                "text": message.text,
                "question": message.question,
                "author": message.author,
                "date": message.date,
                "source_kind": message.source_kind,
            }
            await self.vectors.upsert(
                [VectorRecord(id=vector_id, values=values, metadata=metadata)]
            )
            await self.manifest.mark_active(vector_id, None, self.clock.now())
        except ModelUnavailableError:
            await self.manifest.mark_failed(
                vector_id, None, "model_unavailable", self.clock.now()
            )
            raise
        except (RuntimeError, ValueError):
            await self.manifest.mark_failed(
                vector_id, None, "vector_write_failed", self.clock.now()
            )
            raise ProjectionError("vector_write_failed") from None

    async def remove(self, vector_ids: list[str]) -> None:
        """Delete vectors and their manifest entries."""
        if vector_ids:
            await self.vectors.delete(vector_ids)
            await self.manifest.delete(vector_ids)

    async def repair(self, limit: int = 100) -> ProjectionRepairReport:
        """Repair a bounded batch of pending or failed projections."""
        repaired = removed = failed = 0
        entries = await self.manifest.list_by_state(
            [ProjectionState.PENDING, ProjectionState.FAILED], limit
        )
        for entry in entries:
            if self.budget is not None and not await self.budget.work_allowed(
                AiWorkClass.MAINTENANCE
            ):
                return ProjectionRepairReport(repaired, removed, failed, True)
            try:
                if entry.kind == "qa":
                    item = await self.source.get_current_qa_by_item_id(entry.object_id)
                    if item is None:
                        await self.remove([entry.vector_id])
                        removed += 1
                    else:
                        await self.project_qa(item)
                        repaired += 1
                else:
                    message = await self.source.get_indexable_message(entry.object_id)
                    if message is None:
                        await self.remove([entry.vector_id])
                        removed += 1
                    else:
                        await self.project_message(message)
                        repaired += 1
            except (ProjectionError, ModelUnavailableError, RuntimeError, ValueError):
                failed += 1
        return ProjectionRepairReport(repaired, removed, failed)
