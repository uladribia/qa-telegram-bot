# SPDX-License-Identifier: MIT
"""Index only background messages that are actual evidence."""

from dataclasses import replace

from knowledge_bot.domain.entities import Message
from knowledge_bot.domain.enums import IndexStatus, IntentLabel
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.index import SearchProjectionRepository
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    MessageRepository,
    SourceRepository,
)
from knowledge_bot.ports.vector_store import VectorRecord, VectorStore


class BackgroundIndexer:
    """Immediately project eligible standalone updates and question-answer pairs."""

    def __init__(
        self,
        messages: MessageRepository,
        conversations: ConversationRepository,
        sources: SourceRepository,
        embedder: Embedder,
        vectors: VectorStore,
        manifest: SearchProjectionRepository,
        clock: Clock,
        answer_threshold: float,
    ) -> None:
        """Wire the stores and threshold used by background evidence indexing."""
        self._messages = messages
        self._conversations = conversations
        self._sources = sources
        self._embedder = embedder
        self._vectors = vectors
        self._manifest = manifest
        self._clock = clock
        self._answer_threshold = answer_threshold

    async def process(
        self, message_id: str, message_embedding: tuple[float, ...]
    ) -> bool:
        """Index an eligible message and persist its projection state."""
        message = await self._messages.get(message_id)
        if message is None or not self._eligible(message):
            if message is not None:
                await self._messages.save(
                    replace(message, index_status=IndexStatus.NOT_ELIGIBLE)
                )
            return False
        source = await self._sources.get(message.source_id)
        conversation = await self._conversations.get(message.conversation_id)
        if source is None or conversation is None or message.text is None:
            return False
        if message.context_question is not None:
            values = (
                await self._embedder.embed(
                    [f"Question: {message.context_question}\nAnswer: {message.text}"]
                )
            )[0]
        else:
            if not message_embedding:
                return False
            values = list(message_embedding)
        if not values:
            return False
        record = VectorRecord(
            id=f"msg:{message.id}",
            values=values,
            metadata={
                "kind": "message_evidence",
                "object_id": message.id,
                "source_kind": source.source_type,
                "authority": max(source.authority, message.sender_authority or 0),
                "scope_key": (
                    scope_for_space(conversation.space_id)
                    if conversation.space_id is not None
                    else GLOBAL_SCOPE
                ),
                "text": message.text,
                "question": message.context_question,
                "author": message.sender_name,
                "date": message.sent_at.isoformat(),
            },
        )
        await self._messages.save(replace(message, index_status=IndexStatus.PENDING))
        try:
            await self._vectors.upsert([record])
            await self._manifest.record([record], self._clock.now())
        except (RuntimeError, ValueError):
            await self._messages.save(replace(message, index_status=IndexStatus.FAILED))
            raise
        await self._messages.save(
            replace(
                message,
                index_status=IndexStatus.INDEXED,
                indexed_at=self._clock.now(),
            )
        )
        return True

    def _eligible(self, message: Message) -> bool:
        if message.context_question is not None:
            return True
        score = message.intent_score or 0.0
        return (
            message.intent_label
            in {
                IntentLabel.KNOWLEDGE_UPDATE.value,
                IntentLabel.CORRECTION.value,
            }
            and score >= self._answer_threshold
        )
