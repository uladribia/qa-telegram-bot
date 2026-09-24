# SPDX-License-Identifier: MIT
"""Ingest use case: persist a normalized message and its attachments.

Ingestion is idempotent: re-processing the same inbound event (Telegram may
retry webhooks) must not duplicate state.
"""

import json
from dataclasses import dataclass

from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.domain.entities import Attachment, Conversation, Message, Source
from knowledge_bot.domain.enums import ClassificationStatus, IndexStatus
from knowledge_bot.domain.scope import GLOBAL_SCOPE, Scope
from knowledge_bot.ports.repositories import (
    AttachmentRepository,
    ConversationRepository,
    MessageRepository,
    SourceRepository,
)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of ingesting one message."""

    message_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class MessageIngestor:
    """Persist normalized messages without duplicating them."""

    sources: SourceRepository
    conversations: ConversationRepository
    messages: MessageRepository
    attachments: AttachmentRepository

    async def ingest(
        self,
        message: NormalizedMessage,
        source_scope: Scope = GLOBAL_SCOPE,
        *,
        intent_label: str | None = None,
        intent_score: float | None = None,
        context_question: str | None = None,
        classification_status: ClassificationStatus = (
            ClassificationStatus.NOT_CLASSIFIED
        ),
        intent_scores: dict[str, float] | None = None,
        index_status: IndexStatus = IndexStatus.NOT_INDEXED,
    ) -> IngestResult:
        """Persist a normalized message and its attachments.

        Args:
            message: The normalized inbound message.
            source_scope: Scope for the backing source row, if newly created.
            intent_label: The listener's winning intent label, if classified.
            intent_score: The winning label's similarity score, if classified.
            context_question: The matched parent question, for paired answers.
            classification_status: Background classification lifecycle state.
            intent_scores: All intent similarities for audit/debugging.
            index_status: Evidence-index lifecycle state.

        Returns:
            The stored message id and whether it was newly created.
        """
        source = message.source
        source_id = source.id
        if await self.sources.get(source_id) is None:
            await self.sources.add(
                Source(
                    id=source_id,
                    source_type=source.kind,
                    authority=source.authority,
                    created_at=message.timestamp,
                    title=source.kind,
                    is_mutable=True,
                    scope_key=source_scope,
                )
            )
        if await self.conversations.get(message.conversation_id) is None:
            await self.conversations.add(
                Conversation(
                    id=message.conversation_id,
                    source_id=source_id,
                    space_id=message.space_id,
                    created_at=message.timestamp,
                    external_id=message.conversation_id,
                )
            )
        created = await self.messages.add(
            Message(
                id=message.id,
                source_id=source_id,
                conversation_id=message.conversation_id,
                content_type=message.content_type,
                sent_at=message.timestamp,
                created_at=message.timestamp,
                sender_is_admin=message.sender_is_admin,
                sender_authority=message.sender_authority,
                external_id=message.source_message_id,
                sender_hash=message.sender_id,
                sender_name=message.sender_name,
                text=message.text,
                reply_to_message_id=message.reply_to_message_id,
                intent_label=intent_label,
                intent_score=intent_score,
                context_question=context_question,
                classification_status=classification_status,
                intent_scores_json=(
                    json.dumps(intent_scores, sort_keys=True)
                    if intent_scores is not None
                    else None
                ),
                index_status=index_status,
            )
        )
        if not created:
            return IngestResult(message_id=message.id, created=False)
        for index, reference in enumerate(message.attachments):
            await self.attachments.add(
                Attachment(
                    id=f"{message.id}:{index}",
                    message_id=message.id,
                    kind=reference.kind,
                    processing_status=reference.processing_status,
                    created_at=message.timestamp,
                    external_file_id=reference.external_id,
                    file_name=reference.file_name,
                    mime_type=reference.mime_type,
                    width=reference.width,
                    height=reference.height,
                    size_bytes=reference.size_bytes,
                )
            )
        return IngestResult(message_id=message.id, created=True)
