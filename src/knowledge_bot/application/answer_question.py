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
from knowledge_bot.contracts.api import (
    AnswerSource,
    AskQuestionRequest,
    AskQuestionResponse,
)
from knowledge_bot.contracts.messages import NormalizedMessage
from knowledge_bot.domain.entities import (
    BotAnswer,
    Conversation,
    DeliveryReceipt,
    Source,
)
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.generator import (
    EvidenceItem,
    GenerationRequest,
    Generator,
)
from knowledge_bot.ports.repositories import (
    BotAnswerRepository,
    ConversationRepository,
    DeliveryReceiptRepository,
    SourceRepository,
)
from knowledge_bot.ports.transport import MessageTransport

ABSTENTION_TEXT = "No tinc prou informació fiable per respondre-ho."
UNAVAILABLE_TEXT = (
    "Ara mateix no puc consultar la informació. Torna-ho a provar en una estona."
)


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
    """The decided answer, its mode, provenance, and text to send."""

    answer: str
    mode: AnswerMode
    source_ids: list[str]
    text: str
    qa_version_id: str | None = None


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
    if source.author and source.url:
        parts.append(source.author)
    return "\u2022 " + " \u00b7 ".join(parts)


def _render(answer: str, sources: list[Evidence]) -> str:
    if not sources:
        return answer
    lines = [answer, "", "Fonts:"]
    lines.extend(render_source_line(source) for source in sources)
    return "\n".join(lines)


def _api_response(record: BotAnswer, preview: "AnswerPreview") -> AskQuestionResponse:
    """Convert an internal answer outcome to the channel-neutral API contract."""
    cited_ids = set(preview.outcome.source_ids)
    sources = [
        AnswerSource(
            source_id=item.source_id,
            kind="qa" if item.qa_version_id is not None else "message",
            label=item.label,
            url=item.url,
            author=item.author,
            date=item.date,
        )
        for item in preview.evidence
        if item.source_id in cited_ids
    ]
    return AskQuestionResponse(
        answer_id=record.id,
        mode=record.answer_mode,
        answer=record.answer,
        rendered_text=preview.outcome.text,
        sources=sources,
    )


def _abstain() -> AnswerOutcome:
    return AnswerOutcome(
        answer=ABSTENTION_TEXT,
        mode=AnswerMode.ABSTENTION,
        source_ids=[],
        text=ABSTENTION_TEXT,
    )


@dataclass(frozen=True, slots=True)
class AnswerPreview:
    """A decided answer plus the evidence it was decided from."""

    outcome: AnswerOutcome
    evidence: list[Evidence]


@dataclass(frozen=True, slots=True)
class AnswerService:
    """Decide, persist, and send an answer to an addressed message."""

    retrieval: RetrievalService
    generator: Generator
    answers: BotAnswerRepository
    delivery_receipts: DeliveryReceiptRepository
    transport: MessageTransport
    channel: str
    clock: Clock
    direct_qa_threshold: float = 0.7
    synthesis_threshold: float = 0.3
    conversations: ConversationRepository | None = None
    sources: SourceRepository | None = None

    async def get_answer(self, answer_id: str) -> BotAnswer | None:
        """Return a stored answer for correction and API flows."""
        return await self.answers.get(answer_id)

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
            item
            for item in retrieved.qa
            if item.similarity >= self.direct_qa_threshold
            and item.qa_version_id is not None
        ]
        if strong_qa:
            best = max(strong_qa, key=lambda item: item.similarity)
            return AnswerOutcome(
                answer=best.text,
                mode=AnswerMode.DIRECT_QA,
                source_ids=[best.source_id],
                text=_render(best.text, [best]),
                qa_version_id=best.qa_version_id,
            )
        qa_evidence = sorted(
            (
                item
                for item in retrieved.qa
                if item.similarity >= self.synthesis_threshold
            ),
            key=lambda item: (item.similarity, item.authority),
            reverse=True,
        )[:2]
        message_evidence = sorted(
            (
                item
                for item in retrieved.messages
                if item.similarity >= self.synthesis_threshold
            ),
            key=lambda item: (item.similarity, item.authority),
            reverse=True,
        )[:3]
        evidence = [*qa_evidence, *message_evidence]
        if not evidence:
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

    async def answer_request(self, request: AskQuestionRequest) -> AskQuestionResponse:
        """Answer an idempotent channel-independent API request without delivery."""
        existing = await self.answers.get_by_request_id(request.request_id)
        if existing is not None:
            if existing.question != request.question.strip():
                message = "request_id was already used for another question"
                raise ValueError(message)
            return AskQuestionResponse(
                answer_id=existing.id,
                mode=existing.answer_mode,
                answer=existing.answer,
                rendered_text=existing.answer,
                sources=[],
            )
        question = request.question.strip()
        try:
            retrieved = await self.retrieval.retrieve(question, request.space_id)
            preview = AnswerPreview(
                outcome=await self.decide(question, retrieved),
                evidence=retrieved.all(),
            )
        except ModelUnavailableError:
            outcome = AnswerOutcome(
                answer=UNAVAILABLE_TEXT,
                mode=AnswerMode.UNAVAILABLE,
                source_ids=[],
                text=UNAVAILABLE_TEXT,
            )
            preview = AnswerPreview(outcome=outcome, evidence=[])
        record = BotAnswer(
            id=f"ans:req:{request.request_id}",
            conversation_id=f"api:{request.space_id or 'global'}",
            space_id=request.space_id,
            question=request.question.strip(),
            answer=preview.outcome.answer,
            answer_mode=preview.outcome.mode,
            created_at=self.clock.now(),
            qa_version_id=preview.outcome.qa_version_id,
            sources_json=json.dumps(preview.outcome.source_ids),
            request_id=request.request_id,
        )
        await self._ensure_api_conversation(request.space_id)
        await self.answers.add(record)
        return _api_response(record, preview)

    async def _ensure_api_conversation(self, space_id: str | None) -> None:
        """Create the durable conversation used by generic API answers."""
        if self.conversations is None or self.sources is None:
            return
        source_id = "source:api"
        if await self.sources.get(source_id) is None:
            await self.sources.add(
                Source(
                    id=source_id,
                    source_type="api",
                    authority=0,
                    created_at=self.clock.now(),
                )
            )
        conversation_id = f"api:{space_id or 'global'}"
        if await self.conversations.get(conversation_id) is None:
            await self.conversations.add(
                Conversation(
                    id=conversation_id,
                    source_id=source_id,
                    space_id=space_id,
                    created_at=self.clock.now(),
                    external_id=conversation_id,
                    title="Generic API",
                )
            )

    async def dry_run_for_message(self, message: NormalizedMessage) -> AnswerPreview:
        """Resolve and decide a normalized message without persistence or delivery."""
        question = clean_question(message.text)
        retrieved = await self.retrieval.retrieve(question, message.space_id)
        return AnswerPreview(
            outcome=await self.decide(question, retrieved),
            evidence=retrieved.all(),
        )

    async def dry_run(self, question: str) -> AnswerPreview:
        """Decide an answer without persisting or sending it (eval only).

        Args:
            question: The bare question to answer.

        Returns:
            The decided outcome plus the retrieved evidence behind it.
        """
        cleaned = clean_question(question)
        retrieved = await self.retrieval.retrieve(cleaned, all_scopes=True)
        outcome = await self.decide(cleaned, retrieved)
        return AnswerPreview(outcome=outcome, evidence=retrieved.all())

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
        prior_delivery = await self.delivery_receipts.get(
            "answer", f"ans:{message.id}", self.channel
        )
        if prior_delivery is not None:
            return await self.answers.get(f"ans:{message.id}")
        try:
            retrieved = await self.retrieval.retrieve(question, message.space_id)
            outcome = await self.decide(question, retrieved)
        except ModelUnavailableError:
            outcome = AnswerOutcome(
                answer=UNAVAILABLE_TEXT,
                mode=AnswerMode.UNAVAILABLE,
                source_ids=[],
                text=UNAVAILABLE_TEXT,
            )
        record = BotAnswer(
            id=f"ans:{message.id}",
            conversation_id=message.conversation_id,
            space_id=message.space_id,
            question=question,
            answer=outcome.answer,
            answer_mode=outcome.mode,
            created_at=self.clock.now(),
            user_message_id=message.id,
            qa_version_id=outcome.qa_version_id,
            sources_json=json.dumps(outcome.source_ids),
        )
        message_id = await self.transport.send_answer(
            message.conversation_id, outcome.text, record.id
        )
        if message_id is not None:
            record = BotAnswer(
                id=record.id,
                conversation_id=record.conversation_id,
                space_id=record.space_id,
                question=record.question,
                answer=record.answer,
                answer_mode=record.answer_mode,
                created_at=record.created_at,
                user_message_id=record.user_message_id,
                telegram_bot_message_id=message_id,
                qa_version_id=record.qa_version_id,
                sources_json=record.sources_json,
            )
        await self.answers.add(record)
        if message_id is not None:
            await self.delivery_receipts.add(
                DeliveryReceipt(
                    id=f"delivery:{record.id}:{self.channel}",
                    object_type="answer",
                    object_id=record.id,
                    channel=self.channel,
                    external_conversation_id=message.conversation_id,
                    external_message_id=message_id,
                    created_at=self.clock.now(),
                )
            )
        return record
