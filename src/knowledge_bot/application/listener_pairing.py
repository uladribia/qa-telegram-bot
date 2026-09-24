# SPDX-License-Identifier: MIT
"""Conservative deterministic pairing of listener questions and answers.

No model call is made to pair ordinary messages. An explicit Telegram reply
is paired at ingest time (``app.py``); this service handles the temporal
case: a confident standalone update or correction is paired only when the
conversation has exactly one plausible unresolved recent question.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import timedelta

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.classifier import message_is_confident
from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.domain.entities import Message, MessagePairCandidate
from knowledge_bot.domain.enums import AiWorkClass, IndexStatus
from knowledge_bot.domain.errors import ModelUnavailableError, ProjectionError
from knowledge_bot.domain.policies import effective_message_authority
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.index import IndexableMessage
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    MessagePairCandidateRepository,
    MessageRepository,
    SourceRepository,
)

_QUESTION_LABEL = "question"
_ANSWER_LABELS = frozenset({"knowledge_update", "correction"})
_MAX_QUESTION_CHARS = 500


@dataclass(frozen=True, slots=True)
class MessagePairingService:
    """Pair confident answers with one unambiguous recent question."""

    messages: MessageRepository
    conversations: ConversationRepository
    sources: SourceRepository
    candidates: MessagePairCandidateRepository
    projector: SearchProjectionService
    clock: Clock
    budget: AiBudget | None = None
    question_window_minutes: int = 5
    max_pending_questions: int = 5
    confidence_threshold: float = 0.60
    margin_threshold: float = 0.15

    async def on_message(self, message_id: str) -> int:
        """Evaluate one freshly classified message for temporal pairing.

        High-confidence questions stay as pending question candidates (they
        are never indexed as evidence). Confident standalone updates or
        corrections pair only when exactly one plausible unresolved question
        is recent enough; anything else remains a standalone factual update.
        """
        message = await self.messages.get(message_id)
        if message is None or not message.text:
            return 0
        if not message_is_confident(
            message.intent_label,
            message.intent_score,
            message.intent_scores_json,
            confidence_threshold=self.confidence_threshold,
            margin_threshold=self.margin_threshold,
        ):
            return 0
        if (
            message.intent_label not in _ANSWER_LABELS
            or message.context_question is not None
        ):
            return 0
        if self.budget is not None and not await self.budget.work_allowed(
            AiWorkClass.BACKGROUND
        ):
            return 0
        questions = await self._recent_questions(message)
        if len(questions) != 1:
            # 0 plausible questions: standalone update. More than one:
            # ambiguous, refuse to pair automatically.
            return 0
        return await self._pair(message, questions[0])

    async def _recent_questions(self, answer: Message) -> list[Message]:
        """Return confident unresolved questions inside the pairing window."""
        since = answer.created_at - timedelta(minutes=self.question_window_minutes)
        candidates = await self.messages.list_recent_unpaired_questions(
            answer.conversation_id, since, answer.created_at, self.max_pending_questions
        )
        return [
            question
            for question in candidates
            if question.id != answer.id
            and message_is_confident(
                question.intent_label,
                question.intent_score,
                question.intent_scores_json,
                confidence_threshold=self.confidence_threshold,
                margin_threshold=self.margin_threshold,
            )
        ]

    async def _pair(self, answer: Message, question: Message) -> int:
        """Record the pair, mark the answer as paired evidence, and project it."""
        candidate = MessagePairCandidate(
            id=self._candidate_id(answer.conversation_id, question.id, answer.id),
            conversation_id=answer.conversation_id,
            question_message_id=question.id,
            answer_message_id=answer.id,
            confidence=answer.intent_score or 0.0,
            source="deterministic_reply_window",
            created_at=self.clock.now(),
        )
        if not await self.candidates.add(candidate):
            return 0
        updated = replace(
            answer,
            context_question=question.text[:_MAX_QUESTION_CHARS]
            if question.text
            else None,
            index_status=IndexStatus.PENDING,
        )
        await self.messages.save(updated)
        try:
            await self._project_answer(updated, question)
        except (ProjectionError, ModelUnavailableError, RuntimeError, ValueError):
            await self.messages.save(replace(updated, index_status=IndexStatus.FAILED))
        return 1

    async def _project_answer(self, answer: Message, question: Message) -> None:
        """Project the accepted answer under its stable message id."""
        source = await self.sources.get(answer.source_id)
        conversation = await self.conversations.get(answer.conversation_id)
        if source is None or conversation is None or not answer.text:
            return
        await self.projector.project_message(
            IndexableMessage(
                message_id=answer.id,
                text=answer.text,
                source_kind=source.source_type,
                authority=effective_message_authority(
                    source.authority, answer.sender_authority
                ),
                conversation_id=answer.conversation_id,
                scope_key=scope_for_space(conversation.space_id)
                if conversation.space_id is not None
                else GLOBAL_SCOPE,
                author=answer.sender_name,
                date=answer.sent_at.isoformat(),
                question=question.text,
            )
        )
        await self.messages.save(
            replace(
                answer, index_status=IndexStatus.INDEXED, indexed_at=self.clock.now()
            )
        )

    @staticmethod
    def _candidate_id(conversation_id: str, question_id: str, answer_id: str) -> str:
        """Return a stable candidate id."""
        return hashlib.sha256(
            f"{conversation_id}:{question_id}:{answer_id}".encode()
        ).hexdigest()[:24]
