# SPDX-License-Identifier: MIT
"""Ingestion of group messages that were not addressed to the bot.

A listened conversation is classified and stored either way, so a later
question can be answered from it. Nothing here is channel-specific: the caller
arrives with a normalized message and an intake decision, whichever connector
produced them.

One message costs one assessment. The open questions it could answer are
collected first and travel with that single request, so a decision model sees
the intent and every candidate relevance together and nothing is classified
twice.
"""

from dataclasses import dataclass

from knowledge_bot.application.background import BackgroundIndexer
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.ingest import MessageIngestor
from knowledge_bot.application.listener_pairing import MessagePairingService
from knowledge_bot.domain.enums import AiWorkClass, ClassificationStatus, IndexStatus
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.ports.assessment import MessageAssessmentModel

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
    assessment: MessageAssessmentModel
    budget: AiBudget
    pairing: MessagePairingService
    background_indexer: BackgroundIndexer

    async def handle(self, message: NormalizedMessage) -> ListenerResult:
        """Assess and ingest an unaddressed group message.

        Only clear-cut chitchat is discarded; everything else is kept as
        context, and a message that answers an open question is matched into a
        question-answer pair. Media-only messages carry no text to score, so
        they are kept unlabeled.

        A message is a confident question only when the assessment says so.
        Every early return below is a message the assessment never saw as one:
        no text, or no background budget.

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
        candidates = await self.pairing.candidates_for(message)
        assessment = await self.assessment.assess(text, candidates=candidates)
        classification = assessment.classification
        scores = classification.scores
        selected = self.pairing.select_pair(message, assessment, candidates)
        context_question = (
            selected.question[:MAX_LISTENER_QUESTION_CHARS] if selected else None
        )
        result = await self.ingestor.ingest(
            message,
            intent_label=classification.best_label.value,
            intent_score=classification.best_score,
            context_question=context_question,
            classification_status=(
                ClassificationStatus.PREFILTER_CHITCHAT
                if classification.prefiltered
                else ClassificationStatus.CLASSIFIED
            ),
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
            stored = await self.ingestor.get_message(message.id)
            if stored is not None and selected is not None:
                await self.pairing.record_pair(stored, selected)
            else:
                await self.background_indexer.process(
                    message.id, classification.embedding
                )
        status = INGEST_PAIR_STATUS if selected is not None else INGEST_STATUS
        return ListenerResult(status, self.assessment.is_question(classification))
