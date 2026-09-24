# SPDX-License-Identifier: MIT
"""Batched temporal question-answer pairing for listener messages."""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from knowledge_bot.domain.entities import (
    ListenerPairingWindow,
    Message,
    MessagePairCandidate,
)
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.index import SearchProjectionRepository
from knowledge_bot.ports.pairing import PairingModel, PairMessage
from knowledge_bot.ports.repositories import (
    ConversationRepository,
    ListenerPairingWindowRepository,
    MessagePairCandidateRepository,
    MessageRepository,
)
from knowledge_bot.ports.vector_store import VectorRecord, VectorStore


@dataclass(frozen=True, slots=True)
class MessagePairingService:
    """Extract and index candidate pairs from bounded conversation windows."""

    messages: MessageRepository
    conversations: ConversationRepository
    candidates: MessagePairCandidateRepository
    windows: ListenerPairingWindowRepository
    embedder: Embedder
    vectors: VectorStore
    manifest: SearchProjectionRepository
    model: PairingModel
    clock: Clock
    window_minutes: int = 10
    quiet_minutes: int = 2
    overlap_minutes: int = 3
    max_messages: int = 20
    min_confidence: float = 0.60

    async def on_message(self, message_id: str) -> int:
        """Accumulate a message and flush only windows past their quiet period."""
        message = await self.messages.get(message_id)
        if message is None or not message.text:
            return 0
        now = self.clock.now()
        window = await self.windows.get(message.conversation_id)
        if window is None:
            window = ListenerPairingWindow(
                id=f"window:{message.conversation_id}",
                conversation_id=message.conversation_id,
                started_at=now,
                last_message_at=now,
            )
        else:
            window = replace(window, last_message_at=now)
        await self.windows.save(window)
        return await self.flush_due(now)

    async def flush_due(self, now: datetime | None = None) -> int:
        """Flush every conversation window whose quiet period has elapsed."""
        current = now or self.clock.now()
        accepted = 0
        for window in await self.windows.list_due(
            current - timedelta(minutes=self.quiet_minutes)
        ):
            message = await self.messages.get(window.conversation_id + ":latest")
            if message is None:
                recent = await self.messages.list_recent(
                    window.conversation_id,
                    window.started_at - timedelta(minutes=self.overlap_minutes),
                    self.max_messages,
                )
                if recent:
                    message = recent[-1]
            if message is not None:
                accepted += await self.process_message(message.id)
            await self.windows.save(
                replace(window, processed_at=current, status="processed")
            )
        return accepted

    async def process_message(self, message_id: str) -> int:
        """Process the recent window after one listener message is stored."""
        message = await self.messages.get(message_id)
        if message is None or not message.text:
            return 0
        start = message.created_at - timedelta(
            minutes=self.window_minutes + self.overlap_minutes
        )
        recent = await self.messages.list_recent(
            message.conversation_id, start, self.max_messages
        )
        accepted = 0
        for window in self._windows(recent):
            if len(window) < 2:
                continue
            output = await self.model.pair(
                [
                    PairMessage(
                        id=item.id,
                        text=item.text or "",
                        created_at=item.created_at,
                        reply_to_message_id=item.reply_to_message_id,
                    )
                    for item in window
                ]
            )
            conversation = await self.conversations.get(message.conversation_id)
            scope = (
                scope_for_space(conversation.space_id)
                if conversation and conversation.space_id is not None
                else GLOBAL_SCOPE
            )
            by_id = {item.id: item for item in window}
            for pair in output.pairs:
                if pair.confidence < self.min_confidence:
                    continue
                question = by_id.get(pair.question_id)
                answer = by_id.get(pair.answer_id)
                if question is None or answer is None or question.id == answer.id:
                    continue
                candidate = MessagePairCandidate(
                    id=self._candidate_id(
                        conversation_id := message.conversation_id,
                        question.id,
                        answer.id,
                    ),
                    conversation_id=conversation_id,
                    question_message_id=question.id,
                    answer_message_id=answer.id,
                    confidence=pair.confidence,
                    source="temporal_window_llm",
                    created_at=self.clock.now(),
                )
                if not await self.candidates.add(candidate):
                    continue
                values = (
                    await self.embedder.embed(
                        [f"Question: {question.text}\nAnswer: {answer.text}"]
                    )
                )[0]
                record = VectorRecord(
                    id=f"pair:{candidate.id}",
                    values=values,
                    metadata={
                        "kind": "message_evidence_pair",
                        "object_id": candidate.id,
                        "scope_key": scope,
                        "question": question.text,
                        "text": answer.text,
                        "confidence": candidate.confidence,
                    },
                )
                await self.vectors.upsert([record])
                await self.manifest.record([record], self.clock.now())
                accepted += 1
        return accepted

    def _windows(self, messages: list[Message]) -> list[list[Message]]:
        """Split recent messages on idle gaps and hard size limits."""
        windows: list[list[Message]] = []
        current: list[Message] = []
        for message in sorted(messages, key=lambda item: item.created_at):
            if current and (
                message.created_at - current[-1].created_at
                > timedelta(minutes=self.window_minutes)
                or len(current) >= self.max_messages
            ):
                windows.append(current)
                current = []
            current.append(message)
        if current:
            windows.append(current)
        return windows

    @staticmethod
    def _candidate_id(conversation_id: str, question_id: str, answer_id: str) -> str:
        """Return a stable id for one candidate pair."""
        value = f"{conversation_id}:{question_id}:{answer_id}".encode()
        return hashlib.sha256(value).hexdigest()[:24]
