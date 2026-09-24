# SPDX-License-Identifier: MIT
"""Batched temporal question-answer pairing for listener messages."""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from knowledge_bot.application.budget import AiBudget
from knowledge_bot.application.indexing import SearchProjectionService
from knowledge_bot.domain.entities import (
    ListenerPairingWindow,
    Message,
    MessagePairCandidate,
)
from knowledge_bot.domain.enums import AiWorkClass, IndexStatus
from knowledge_bot.domain.errors import ModelUnavailableError, ProjectionError
from knowledge_bot.domain.policies import effective_message_authority
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.index import IndexableMessage
from knowledge_bot.ports.pairing import PairingModel, PairMessage
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    ListenerPairingWindowRepository,
    MessagePairCandidateRepository,
    MessageRepository,
    SourceRepository,
)


@dataclass(frozen=True, slots=True)
class MessagePairingService:
    """Extract accepted pairs as ordinary stable message evidence."""

    messages: MessageRepository
    conversations: ConversationRepository
    sources: SourceRepository
    candidates: MessagePairCandidateRepository
    windows: ListenerPairingWindowRepository
    projector: SearchProjectionService
    model: PairingModel
    clock: Clock
    budget: AiBudget | None = None
    window_minutes: int = 10
    quiet_minutes: int = 2
    overlap_minutes: int = 3
    max_messages: int = 20
    min_confidence: float = 0.60

    async def on_message(self, message_id: str) -> int:
        """Close an old window before starting or extending the new one."""
        message = await self.messages.get(message_id)
        if message is None or not message.text:
            return 0
        now = self.clock.now()
        window = await self.windows.get(message.conversation_id)
        if window is not None and now - window.last_message_at >= timedelta(
            minutes=self.quiet_minutes
        ):
            if not await self._process_window(window):
                return 0
            window = None
        if window is None:
            await self.windows.save(
                ListenerPairingWindow(
                    id=f"window:{message.conversation_id}",
                    conversation_id=message.conversation_id,
                    started_at=now,
                    last_message_at=now,
                )
            )
        else:
            await self.windows.save(replace(window, last_message_at=now))
        return 0

    async def flush_due(self, now: datetime | None = None) -> int:
        """Flush due windows explicitly."""
        current = now or self.clock.now()
        accepted = 0
        for window in await self.windows.list_due(
            current - timedelta(minutes=self.quiet_minutes)
        ):
            accepted += await self._process_window(window)
        return accepted

    async def _process_window(self, window: ListenerPairingWindow) -> int:
        """Process one pending window and mark it only after a successful attempt."""
        messages = await self.messages.list_recent_listener(
            window.conversation_id,
            window.started_at - timedelta(minutes=self.overlap_minutes),
            self.max_messages,
        )
        messages = [
            item for item in messages if item.created_at <= window.last_message_at
        ]
        if len(messages) < 2:
            await self.windows.save(
                replace(window, processed_at=self.clock.now(), status="processed")
            )
            return 0
        if self.budget is not None and not await self.budget.work_allowed(
            AiWorkClass.BACKGROUND
        ):
            return 0
        try:
            output = await self.model.pair(
                [
                    PairMessage(
                        item.id,
                        item.text or "",
                        item.created_at,
                        item.reply_to_message_id,
                    )
                    for item in messages
                ]
            )
        except (ModelUnavailableError, RuntimeError, ValueError):
            return 0
        by_id = {item.id: item for item in messages}
        accepted = 0
        for pair in sorted(
            output.pairs, key=lambda item: item.confidence, reverse=True
        ):
            if pair.confidence < self.min_confidence:
                continue
            question = by_id.get(pair.question_id)
            answer = by_id.get(pair.answer_id)
            if (
                question is None
                or answer is None
                or question.id == answer.id
                or answer.context_question is not None
            ):
                continue
            if self.budget is not None and not await self.budget.work_allowed(
                AiWorkClass.BACKGROUND
            ):
                return accepted
            candidate = MessagePairCandidate(
                id=self._candidate_id(window.conversation_id, question.id, answer.id),
                conversation_id=window.conversation_id,
                question_message_id=question.id,
                answer_message_id=answer.id,
                confidence=pair.confidence,
                source="temporal_window_llm",
                created_at=self.clock.now(),
            )
            if not await self.candidates.add(candidate):
                continue
            await self.messages.save(replace(answer, context_question=question.text))
            try:
                await self._project_answer(answer, question)
            except (ProjectionError, ModelUnavailableError, RuntimeError, ValueError):
                await self.messages.save(
                    replace(answer, index_status=IndexStatus.FAILED)
                )
                return accepted
            accepted += 1
        await self.windows.save(
            replace(window, processed_at=self.clock.now(), status="processed")
        )
        return accepted

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
