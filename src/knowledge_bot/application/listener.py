# SPDX-License-Identifier: MIT
"""Ingestion of group messages that were not addressed to the bot.

A listened conversation is classified and stored either way, so a later
question can be answered from it. Nothing here is channel-specific: the caller
arrives with a normalized message and an intake decision, whichever connector
produced them.
"""

from dataclasses import dataclass

from knowledge_bot.application.background import BackgroundIndexer
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import (
    QUESTION,
    Classification,
    MessageClassifier,
    message_is_confident,
)
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.listener_pairing import MessagePairingService
from knowledge_bot.domain.enums import AiWorkClass, ClassificationStatus, IndexStatus
from knowledge_bot.models.messages import NormalizedMessage

# Longest parent question text stored on a matched pair reply.
MAX_LISTENER_QUESTION_CHARS = 500

INGEST_STATUS = "ingest"
INGEST_PAIR_STATUS = "ingest_pair"


@dataclass(frozen=True, slots=True)
class ListenerResult:
    """What the listener did, and whether it saw a confident question.

    ``status`` is the observable outcome and never changes shape. The flag
    exists so a proactive conversation can decide to answer without paying for
    a second classification of the same message.
    """

    status: str
    confident_question: bool = False


@dataclass(frozen=True, slots=True)
class ListenerIngestor:
    """Classify and store traffic nobody addressed to the bot."""

    ingestor: MessageIngestor
    classifier: MessageClassifier
    budget: AiBudget
    pairing: MessagePairingService
    background_indexer: BackgroundIndexer

    async def handle(self, message: NormalizedMessage) -> ListenerResult:
        """Classify and ingest an unaddressed group message.

        Only clear-cut chitchat is discarded; everything else is kept as
        context, and a reply that answers a parent question is matched into a
        question-answer pair. Media-only messages carry no text to score, so
        they are kept unlabeled.

        A message is a confident question only when the classifier says so.
        Every early return below is a message the classifier never saw as one:
        no text, no background budget, or a classification that did not clear
        the confidence and margin thresholds.

        Args:
            message: The unaddressed inbound message.

        Returns:
            The ingest outcome and whether the message was a confident question.
        """
        text = (message.text or "").strip()
        if not text:
            await self.ingestor.ingest(
                message,
                classification_status=ClassificationStatus.NO_TEXT,
                index_status=IndexStatus.NOT_ELIGIBLE,
            )
            return ListenerResult(INGEST_STATUS)
        if not await self.budget.work_allowed(AiWorkClass.BACKGROUND):
            await self.ingestor.ingest(
                message,
                classification_status=ClassificationStatus.DEFERRED_BUDGET,
                index_status=IndexStatus.NOT_INDEXED,
            )
            return ListenerResult(INGEST_STATUS)
        classification = await self.classifier.classify(text)
        scores = classification.scores
        label = classification.best_label.value
        score = classification.best_score
        context_question = await self.match_parent_question(message, classification)
        status = (
            ClassificationStatus.PREFILTER_CHITCHAT
            if not classification.embedding
            else ClassificationStatus.CLASSIFIED
        )
        result = await self.ingestor.ingest(
            message,
            intent_label=label,
            intent_score=score,
            context_question=context_question,
            classification_status=status,
            intent_scores={
                "question": scores.question,
                "knowledge_update": scores.knowledge_update,
                "correction": scores.correction,
                "chitchat": scores.chitchat,
                "margin": classification.margin,
            },
            index_status=IndexStatus.NOT_ELIGIBLE,
        )
        if result.created:
            await self.pairing.on_message(message.id)
            await self.background_indexer.process(message.id, classification.embedding)
        status = INGEST_PAIR_STATUS if context_question is not None else INGEST_STATUS
        return ListenerResult(status, self.classifier.is_question(classification))

    async def match_parent_question(
        self, message: NormalizedMessage, classification: Classification
    ) -> str | None:
        """Return the parent question text when a reply answers a question.

        Args:
            message: The reply being ingested.
            classification: The reply's classification.

        Returns:
            The parent question text, or ``None`` when this is no clear pair.
        """
        if message.reply_to_message_id is None:
            return None
        if not self.classifier.is_answer_like(classification):
            return None
        parent = await self.ingestor.get_message(
            f"{message.conversation_id}:{message.reply_to_message_id}"
        )
        if parent is None or not parent.text:
            return None
        if parent.intent_label != QUESTION:
            return None
        if not message_is_confident(
            parent.intent_label,
            parent.intent_score,
            parent.intent_scores_json,
            confidence_threshold=self.classifier.confidence_threshold,
            margin_threshold=self.classifier.margin_threshold,
        ):
            return None
        return parent.text[:MAX_LISTENER_QUESTION_CHARS]
