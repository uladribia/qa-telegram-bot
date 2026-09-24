# SPDX-License-Identifier: MIT
"""Index only background messages that are actual evidence."""

import json
from dataclasses import dataclass, replace

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import MessageClassifier, message_is_confident
from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.domain.entities import Message
from knowledge_bot.domain.enums import (
    AiWorkClass,
    ClassificationStatus,
    IndexStatus,
    IntentLabel,
)
from knowledge_bot.domain.policies import effective_message_authority
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.index import IndexableMessage
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    MessageRepository,
    SourceRepository,
)


@dataclass(frozen=True, slots=True)
class BackgroundProcessResult:
    """Result of one bounded background maintenance pass."""

    processed: int = 0
    stopped_by_budget: bool = False


class BackgroundIndexer:
    """Immediately project eligible background evidence."""

    def __init__(
        self,
        messages: MessageRepository,
        conversations: ConversationRepository,
        sources: SourceRepository,
        classifier: MessageClassifier,
        projector: SearchProjectionService,
        clock: Clock,
        confidence_threshold: float,
        margin_threshold: float,
        budget: AiBudget | None = None,
    ) -> None:
        """Wire stores and the shared projection service."""
        self._messages = messages
        self._conversations = conversations
        self._sources = sources
        self._classifier = classifier
        self._projector = projector
        self._clock = clock
        self._confidence_threshold = confidence_threshold
        self._margin_threshold = margin_threshold
        self._budget = budget

    async def process_backlog(self, limit: int) -> BackgroundProcessResult:
        """Process deferred messages with a budget check before each item."""
        messages = await self._messages.list_by_classification_status(
            ClassificationStatus.DEFERRED_BUDGET.value, limit
        )
        processed = 0
        for message in messages:
            if self._budget is not None and not await self._budget.work_allowed(
                AiWorkClass.MAINTENANCE
            ):
                return BackgroundProcessResult(processed, True)
            if not message.text:
                await self._messages.save(
                    replace(
                        message,
                        classification_status=ClassificationStatus.NO_TEXT,
                        index_status=IndexStatus.NOT_ELIGIBLE,
                    )
                )
                processed += 1
                continue
            classification = await self._classifier.classify(message.text)
            scores = classification.scores
            await self._messages.save(
                replace(
                    message,
                    intent_label=classification.best_label.value,
                    intent_score=classification.best_score,
                    classification_status=(
                        ClassificationStatus.PREFILTER_CHITCHAT
                        if not classification.embedding
                        else ClassificationStatus.CLASSIFIED
                    ),
                    intent_scores_json=json.dumps(
                        {
                            "question": scores.question,
                            "knowledge_update": scores.knowledge_update,
                            "correction": scores.correction,
                            "chitchat": scores.chitchat,
                            "margin": classification.margin,
                        },
                        sort_keys=True,
                    ),
                    index_status=IndexStatus.NOT_ELIGIBLE,
                )
            )
            await self.process(message.id, classification.embedding)
            processed += 1
        return BackgroundProcessResult(processed)

    async def process(
        self, message_id: str, message_embedding: tuple[float, ...]
    ) -> bool:
        """Project one eligible message and persist its index state."""
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
        indexable = IndexableMessage(
            message_id=message.id,
            text=message.text,
            source_kind=source.source_type,
            authority=effective_message_authority(
                source.authority, message.sender_authority
            ),
            conversation_id=message.conversation_id,
            scope_key=scope_for_space(conversation.space_id)
            if conversation.space_id is not None
            else GLOBAL_SCOPE,
            author=message.sender_name,
            date=message.sent_at.isoformat(),
            question=message.context_question,
        )
        await self._messages.save(replace(message, index_status=IndexStatus.PENDING))
        try:
            # Paired evidence embeds its context question, not the message text,
            # so the precomputed message embedding only applies to standalone
            # factual updates.
            precomputed = (
                None if message.context_question is not None else message_embedding
            )
            await self._projector.project_message(indexable, precomputed)
        except (RuntimeError, ValueError):
            await self._messages.save(replace(message, index_status=IndexStatus.FAILED))
            return False
        await self._messages.save(
            replace(
                message, index_status=IndexStatus.INDEXED, indexed_at=self._clock.now()
            )
        )
        return True

    def _eligible(self, message: Message) -> bool:
        """Return whether a message is factual evidence.

        A paired answer is always eligible; a standalone message is evidence
        only when its classifier decision clears the confidence policy.
        """
        if message.context_question is not None:
            return True
        if message.intent_label not in {
            IntentLabel.KNOWLEDGE_UPDATE.value,
            IntentLabel.CORRECTION.value,
        }:
            return False
        return message_is_confident(
            message.intent_label,
            message.intent_score,
            message.intent_scores_json,
            confidence_threshold=self._confidence_threshold,
            margin_threshold=self._margin_threshold,
        )
