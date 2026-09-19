# SPDX-License-Identifier: MIT
"""Answer a question from retrieved evidence (spec §14-§19).

The evidence gate is deterministic:

- A strong active Q&A match is answered directly, without calling the model.
- Otherwise, coherent evidence goes through grounded generation.
- No evidence, conflicts (reported by the model), or unsupported citations
  produce an abstention.
"""

import json
from dataclasses import dataclass

from knowledge_bot.application.retrieval import (
    Evidence,
    RetrievalService,
    RetrievedEvidence,
)
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.domain.entities import BotAnswer
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest, Generator
from knowledge_bot.ports.repositories import BotAnswerRepository
from knowledge_bot.ports.transport import MessageTransport

ABSTENTION_TEXT = "No tinc prou informació fiable per respondre-ho."


def clean_question(text: str | None) -> str:
    """Strip the ``/ask`` command and leading mentions from a question.

    Args:
        text: The raw message text.

    Returns:
        The bare question.
    """
    value = (text or "").strip()
    if value.lower().startswith("/ask"):
        parts = value.split(maxsplit=1)
        value = parts[1] if len(parts) > 1 else ""
    tokens = value.split()
    while tokens and tokens[0].startswith("@"):
        tokens.pop(0)
    return " ".join(tokens).strip()


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """The decided answer, its mode, and the text to send."""

    answer: str
    mode: AnswerMode
    source_ids: list[str]
    text: str


def render_source_line(source: Evidence) -> str:
    """Render one source line, showing URL or author as appropriate.

    Web sources cite their URL; group sources cite the author. Both add the date.

    Args:
        source: The cited evidence.

    Returns:
        A single bullet line.
    """
    parts: list[str] = [source.label]
    if source.question is not None:
        parts.append(source.question)
    if source.url:
        parts.append(source.url)
    elif source.author:
        parts.append(source.author)
    if source.date:
        parts.append(source.date)
    return "\u2022 " + " \u00b7 ".join(parts)


def _render(answer: str, sources: list[Evidence]) -> str:
    if not sources:
        return answer
    lines = [answer, "", "Fonts:"]
    lines.extend(render_source_line(source) for source in sources)
    return "\n".join(lines)


def _abstain() -> AnswerOutcome:
    return AnswerOutcome(
        answer=ABSTENTION_TEXT,
        mode=AnswerMode.ABSTENTION,
        source_ids=[],
        text=ABSTENTION_TEXT,
    )


@dataclass(frozen=True, slots=True)
class AnswerService:
    """Decide, persist, and send an answer to an addressed message."""

    retrieval: RetrievalService
    generator: Generator
    answers: BotAnswerRepository
    transport: MessageTransport
    clock: Clock
    direct_qa_threshold: float = 0.7
    synthesis_threshold: float = 0.3

    async def decide(
        self, question: str, retrieved: RetrievedEvidence
    ) -> AnswerOutcome:
        """Apply the evidence gate to retrieved evidence.

        Args:
            question: The bare question.
            retrieved: The retrieved Q&A and message evidence.

        Returns:
            The decided answer.
        """
        strong_qa = [
            item for item in retrieved.qa if item.similarity >= self.direct_qa_threshold
        ]
        if strong_qa:
            best = max(strong_qa, key=lambda item: item.similarity)
            return AnswerOutcome(
                answer=best.text,
                mode=AnswerMode.DIRECT_QA,
                source_ids=[best.source_id],
                text=_render(best.text, [best]),
            )
        evidence = retrieved.all()
        if (
            not evidence
            or max(item.similarity for item in evidence) < self.synthesis_threshold
        ):
            return _abstain()
        result = await self.generator.generate(
            GenerationRequest(
                question=question,
                evidence=[
                    EvidenceItem(
                        source_id=item.source_id,
                        text=item.text,
                        label=item.label,
                        authority=item.authority,
                    )
                    for item in evidence
                ],
            )
        )
        allowed = {item.source_id for item in evidence}
        if result.status != "answered" or not result.answer.strip():
            return _abstain()
        if any(source_id not in allowed for source_id in result.source_ids):
            return _abstain()
        cited = [item for item in evidence if item.source_id in result.source_ids]
        return AnswerOutcome(
            answer=result.answer,
            mode=AnswerMode.SYNTHESIS,
            source_ids=result.source_ids,
            text=_render(result.answer, cited),
        )

    async def answer(self, message: NormalizedMessage) -> BotAnswer | None:
        """Answer an addressed message and send it.

        Args:
            message: The addressed inbound message.

        Returns:
            The stored answer, or ``None`` when the message has no question.
        """
        question = clean_question(message.text)
        if not question:
            return None
        retrieved = await self.retrieval.retrieve(question)
        outcome = await self.decide(question, retrieved)
        record = BotAnswer(
            id=f"ans:{message.id}",
            conversation_id=message.conversation_id,
            question=question,
            answer=outcome.answer,
            answer_mode=outcome.mode,
            created_at=self.clock.now(),
            user_message_id=message.id,
            sources_json=json.dumps(outcome.source_ids),
        )
        message_id = await self.transport.send_answer(
            message.conversation_id, outcome.text, record.id
        )
        if message_id is not None:
            record = BotAnswer(
                id=record.id,
                conversation_id=record.conversation_id,
                question=record.question,
                answer=record.answer,
                answer_mode=record.answer_mode,
                created_at=record.created_at,
                user_message_id=record.user_message_id,
                telegram_bot_message_id=message_id,
                sources_json=record.sources_json,
            )
        await self.answers.add(record)
        return record
