# SPDX-License-Identifier: MIT
"""Pairing of listener questions and answers, baseline or model-decided.

Pairing answers one question: which open question, if any, does this message
answer? The baseline answers it deterministically, from an explicit reply or
from the shape of the conversation window. A decision model answers it from the
relevance probabilities it returned for every candidate in the same request
that classified the message. Both paths collect the same candidates first, so
neither can decide on a question the other never saw.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import timedelta

from knowledge_bot.application.classifier import message_is_confident
from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.domain.entities import Message, MessagePairCandidate
from knowledge_bot.domain.enums import IndexStatus, IntentLabel
from knowledge_bot.domain.errors import ModelUnavailableError, ProjectionError
from knowledge_bot.domain.policies import effective_message_authority
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.models.assessment import MessageAssessment, RetroevalCandidate
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.ports.assessment import MessageAssessmentModel
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.index import IndexableMessage
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    MessagePairCandidateRepository,
    MessageRepository,
    SourceRepository,
)

_MAX_QUESTION_CHARS = 500
# The operational confidence policy, applied to a classification that arrives
# from outside this service (an evaluation) rather than from the classifier.
_CONFIDENCE_THRESHOLD = 0.60
_MARGIN_THRESHOLD = 0.15
_EXPLICIT_REPLY = "explicit_reply"
_TEMPORAL_WINDOW = "temporal_window"


def select_pair(
    assessment: MessageAssessment,
    candidates: tuple[RetroevalCandidate, ...],
    *,
    relevance_threshold: float,
    relevance_margin: float,
) -> RetroevalCandidate | None:
    """Return the one candidate an assessment accepts, or none.

    With no relevance opinion the deterministic policy decides: an explicit
    reply wins, and a temporal pairing happens only when exactly one open
    question is plausible. With relevance scores, the strongest candidate wins
    only when it clears the threshold and no runner-up comes within the margin.
    A near tie is a refusal, not a guess.

    This is the whole pairing policy, in one place, so the listener and an
    offline evaluation decide by the same rules rather than by two
    implementations that drift apart.

    Args:
        assessment: The message's intent decision and relevance opinion.
        candidates: The candidates collected for this message.
        relevance_threshold: Minimum relevance for the strongest candidate.
        relevance_margin: Minimum gap to the runner-up candidate.

    Returns:
        The accepted candidate, or ``None`` when nothing is accepted.
    """
    if not candidates:
        return None
    if not _is_answer_like(assessment):
        return None
    explicit = next(
        (item for item in candidates if item.relation == _EXPLICIT_REPLY), None
    )
    if not assessment.pair_relevance:
        if explicit is not None:
            return explicit
        return candidates[0] if len(candidates) == 1 else None
    ranked = sorted(
        (
            (
                candidate.candidate_id,
                assessment.pair_relevance.get(candidate.candidate_id, 0.0),
            )
            for candidate in candidates
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    top_id, top_score = ranked[0]
    if top_score < relevance_threshold:
        return None
    if len(ranked) > 1 and top_score - ranked[1][1] < relevance_margin:
        return None
    return next(
        candidate for candidate in candidates if candidate.candidate_id == top_id
    )


def _is_answer_like(assessment: MessageAssessment) -> bool:
    """Whether the assessment says the message is a factual update or correction."""
    classification = assessment.classification
    if classification.prefiltered:
        return False
    return classification.is_confident_with(
        _CONFIDENCE_THRESHOLD, _MARGIN_THRESHOLD
    ) and classification.best_label in (
        IntentLabel.KNOWLEDGE_UPDATE,
        IntentLabel.CORRECTION,
    )


@dataclass(frozen=True, slots=True)
class MessagePairingService:
    """Collect open questions for a message and decide whether it answers one."""

    messages: MessageRepository
    conversations: ConversationRepository
    sources: SourceRepository
    candidates: MessagePairCandidateRepository
    projector: SearchProjectionService
    assessment: MessageAssessmentModel
    clock: Clock
    question_window_minutes: int = 5
    max_pending_questions: int = 5
    relevance_threshold: float = 0.80
    relevance_margin: float = 0.15

    @property
    def confidence_threshold(self) -> float:
        """The confidence policy applied to stored classifications."""
        return self.assessment.confidence_threshold

    @property
    def margin_threshold(self) -> float:
        """The margin policy applied to stored classifications."""
        return self.assessment.margin_threshold

    async def candidates_for(
        self, message: NormalizedMessage
    ) -> tuple[RetroevalCandidate, ...]:
        """Return every open question this message could answer.

        The explicit parent comes first when it is a confident question; every
        other confident question recent enough in the same conversation follows.
        A parent that is also inside the window appears once, keeping its
        explicit relation.

        This reads only. It makes no model call, writes nothing, and decides
        nothing: the decision model must see all candidates at once.

        Args:
            message: The message being ingested.

        Returns:
            The candidate questions, explicit reply first.
        """
        candidates: list[RetroevalCandidate] = []
        seen: set[str] = set()
        explicit = await self._explicit_candidate(message)
        if explicit is not None:
            candidates.append(explicit)
            seen.add(explicit.question_message_id)
        for question in await self._recent_questions(message):
            if question.id in seen:
                continue
            candidates.append(
                RetroevalCandidate(
                    candidate_id=self._candidate_id(
                        message.conversation_id, question.id, message.id
                    ),
                    question_message_id=question.id,
                    question=question.text or "",
                    relation=_TEMPORAL_WINDOW,
                )
            )
        return tuple(candidates)

    def select_pair(
        self,
        message: NormalizedMessage,
        assessment: MessageAssessment,
        candidates: tuple[RetroevalCandidate, ...],
    ) -> RetroevalCandidate | None:
        """Return the one candidate this message answers, or none.

        Args:
            message: The message being ingested.
            assessment: The message's intent decision and relevance opinion.
            candidates: The candidates collected for this message.

        Returns:
            The accepted candidate, or ``None`` when the message pairs with
            nothing.
        """
        return select_pair(
            assessment,
            candidates,
            relevance_threshold=self.relevance_threshold,
            relevance_margin=self.relevance_margin,
        )

    async def record_pair(self, answer: Message, candidate: RetroevalCandidate) -> int:
        """Record the accepted pair and project the answer as its evidence.

        Args:
            answer: The stored message answering the question.
            candidate: The accepted candidate question.

        Returns:
            ``1`` when a new pair was recorded, ``0`` when it already existed.
        """
        stored = await self.messages.get(candidate.question_message_id)
        if stored is None or not stored.text:
            return 0
        pair = MessagePairCandidate(
            id=self._candidate_id(answer.conversation_id, stored.id, answer.id),
            conversation_id=answer.conversation_id,
            question_message_id=stored.id,
            answer_message_id=answer.id,
            confidence=answer.intent_score or 0.0,
            source=(
                _EXPLICIT_REPLY
                if candidate.relation == _EXPLICIT_REPLY
                else "deterministic_reply_window"
            ),
            created_at=self.clock.now(),
        )
        if not await self.candidates.add(pair):
            return 0
        updated = replace(
            answer,
            context_question=stored.text[:_MAX_QUESTION_CHARS],
            index_status=IndexStatus.PENDING,
        )
        await self.messages.save(updated)
        try:
            await self._project_answer(updated, stored)
        except (ProjectionError, ModelUnavailableError, RuntimeError, ValueError):
            await self.messages.save(replace(updated, index_status=IndexStatus.FAILED))
        return 1

    async def _explicit_candidate(
        self, message: NormalizedMessage
    ) -> RetroevalCandidate | None:
        """Return the parent question this message replies to, when it is one."""
        if message.reply_to_message_id is None:
            return None
        parent = await self.messages.get(
            f"{message.conversation_id}:{message.reply_to_message_id}"
        )
        if parent is None or not parent.text or parent.id == message.id:
            return None
        if parent.intent_label != "question":
            return None
        if not message_is_confident(
            parent.intent_label,
            parent.intent_score,
            parent.intent_scores_json,
            confidence_threshold=self.confidence_threshold,
            margin_threshold=self.margin_threshold,
        ):
            return None
        return RetroevalCandidate(
            candidate_id=self._candidate_id(
                message.conversation_id, parent.id, message.id
            ),
            question_message_id=parent.id,
            question=parent.text,
            relation=_EXPLICIT_REPLY,
        )

    async def _recent_questions(self, message: NormalizedMessage) -> list[Message]:
        """Return confident unresolved questions inside the pairing window."""
        created_at = message.timestamp
        since = created_at - timedelta(minutes=self.question_window_minutes)
        candidates = await self.messages.list_recent_unpaired_questions(
            message.conversation_id, since, created_at, self.max_pending_questions
        )
        return [
            question
            for question in candidates
            if question.id != message.id
            and message_is_confident(
                question.intent_label,
                question.intent_score,
                question.intent_scores_json,
                confidence_threshold=self.confidence_threshold,
                margin_threshold=self.margin_threshold,
            )
        ]

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
