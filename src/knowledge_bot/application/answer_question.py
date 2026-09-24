# SPDX-License-Identifier: MIT
"""Prepare and persist grounded answers; channel adapters deliver them."""

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
from knowledge_bot.domain.entities import BotAnswer, Conversation, Source
from knowledge_bot.domain.enums import AnswerMode
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest, Generator
from knowledge_bot.ports.repositories import (
    BotAnswerRepository,
    ConversationRepository,
    SourceRepository,
)

ABSTENTION_TEXT = "No tinc prou informació fiable per respondre-ho."
UNAVAILABLE_TEXT = (
    "Ara mateix no puc consultar la informació. Torna-ho a provar en una estona."
)


def clean_question(text: str | None) -> str:
    """Strip commands and leading bot mentions from a question."""
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
    """The decided answer, mode, source ids, and rendered text."""

    answer: str
    mode: AnswerMode
    source_ids: list[str]
    text: str
    qa_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class AnswerPreview:
    """A decided answer and its supporting evidence."""

    outcome: AnswerOutcome
    evidence: list[Evidence]


def render_source_line(source: Evidence) -> str:
    """Render one citation line."""
    parts = [source.label]
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
    return "• " + " · ".join(parts)


def _render(answer: str, sources: list[Evidence]) -> str:
    """Render answer text with citations."""
    if not sources:
        return answer
    return "\n".join(
        [answer, "", "Fonts:", *(render_source_line(item) for item in sources)]
    )


def _source_details(
    source_ids: list[str], evidence: list[Evidence]
) -> list[AnswerSource]:
    """Create the immutable structured citation snapshot."""
    cited = set(source_ids)
    return [
        AnswerSource(
            source_id=item.source_id,
            kind="qa" if item.qa_version_id is not None else "message",
            label=item.label,
            url=item.url,
            author=item.author,
            date=item.date,
        )
        for item in evidence
        if item.source_id in cited
    ]


def _api_response(
    record: BotAnswer, preview: AnswerPreview | None = None
) -> AskQuestionResponse:
    """Reconstruct an exact response from durable answer state."""
    if preview is not None:
        sources = _source_details(preview.outcome.source_ids, preview.evidence)
        rendered_text = preview.outcome.text
    else:
        try:
            raw_sources = json.loads(record.source_details_json)
            sources = [AnswerSource.model_validate(item) for item in raw_sources]
        except (TypeError, ValueError, json.JSONDecodeError):
            sources = []
        rendered_text = record.rendered_text or record.answer
    return AskQuestionResponse(
        answer_id=record.id,
        mode=record.answer_mode,
        answer=record.answer,
        rendered_text=rendered_text,
        sources=sources,
    )


def _abstain() -> AnswerOutcome:
    """Return the deterministic abstention outcome."""
    return AnswerOutcome(ABSTENTION_TEXT, AnswerMode.ABSTENTION, [], ABSTENTION_TEXT)


@dataclass(frozen=True, slots=True)
class AnswerService:
    """Decide and persist answers without performing channel delivery."""

    retrieval: RetrievalService
    generator: Generator
    answers: BotAnswerRepository
    clock: Clock
    direct_qa_threshold: float = 0.7
    synthesis_threshold: float = 0.3
    conversations: ConversationRepository | None = None
    sources: SourceRepository | None = None

    async def get_answer(self, answer_id: str) -> BotAnswer | None:
        """Return a stored answer."""
        return await self.answers.get(answer_id)

    async def decide(
        self, question: str, retrieved: RetrievedEvidence
    ) -> AnswerOutcome:
        """Apply direct-answer and grounded-synthesis policy."""
        strong_qa = [
            item
            for item in retrieved.qa
            if item.similarity >= self.direct_qa_threshold
            and item.qa_version_id is not None
        ]
        if strong_qa:
            best = max(strong_qa, key=lambda item: (item.similarity, item.authority))
            return AnswerOutcome(
                answer=best.text,
                mode=AnswerMode.DIRECT_QA,
                source_ids=[best.source_id],
                text=_render(best.text, [best]),
                qa_version_id=best.qa_version_id,
            )
        qa = sorted(
            (
                item
                for item in retrieved.qa
                if item.similarity >= self.synthesis_threshold
            ),
            key=lambda item: (item.similarity, item.authority),
            reverse=True,
        )[:2]
        messages = sorted(
            (
                item
                for item in retrieved.messages
                if item.similarity >= self.synthesis_threshold
            ),
            key=lambda item: (item.similarity, item.authority),
            reverse=True,
        )[:3]
        evidence = [*qa, *messages]
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
        """Answer an idempotent API request and replay its stored response."""
        existing = await self.answers.get_by_request_id(request.request_id)
        if existing is not None:
            if existing.question != request.question.strip():
                raise ValueError("request_id was already used for another question")  # noqa: TRY003
            return _api_response(existing)
        return await self._prepare(
            f"ans:req:{request.request_id}",
            request.question,
            f"api:{request.space_id or 'global'}",
            request.space_id,
            None,
            request.request_id,
        )

    async def answer_message(
        self, message: NormalizedMessage
    ) -> AskQuestionResponse | None:
        """Prepare and persist the answer for an addressed message."""
        question = clean_question(message.text)
        if not question:
            return None
        existing = await self.answers.get(f"ans:{message.id}")
        if existing is not None:
            return _api_response(existing)
        return await self._prepare(
            f"ans:{message.id}",
            question,
            message.conversation_id,
            message.space_id,
            message.id,
            None,
        )

    async def _prepare(
        self,
        answer_id: str,
        question: str,
        conversation_id: str,
        space_id: str | None,
        user_message_id: str | None,
        request_id: str | None,
    ) -> AskQuestionResponse:
        """Retrieve, decide, persist, and return one answer."""
        question = question.strip()
        try:
            retrieved = await self.retrieval.retrieve(question, space_id)
            preview = AnswerPreview(
                await self.decide(question, retrieved), retrieved.all()
            )
        except ModelUnavailableError:
            preview = AnswerPreview(
                AnswerOutcome(
                    UNAVAILABLE_TEXT, AnswerMode.UNAVAILABLE, [], UNAVAILABLE_TEXT
                ),
                [],
            )
        details = _source_details(preview.outcome.source_ids, preview.evidence)
        record = BotAnswer(
            id=answer_id,
            conversation_id=conversation_id,
            space_id=space_id,
            question=question,
            answer=preview.outcome.answer,
            answer_mode=preview.outcome.mode,
            created_at=self.clock.now(),
            user_message_id=user_message_id,
            qa_version_id=preview.outcome.qa_version_id,
            sources_json=json.dumps(preview.outcome.source_ids),
            rendered_text=preview.outcome.text,
            source_details_json=json.dumps(
                [item.model_dump(mode="json") for item in details]
            ),
            request_id=request_id,
        )
        if request_id is not None:
            await self._ensure_api_conversation(space_id)
        await self.answers.add(record)
        return _api_response(record, preview)

    async def _ensure_api_conversation(self, space_id: str | None) -> None:
        """Create the durable conversation used by generic API answers."""
        if self.conversations is None or self.sources is None:
            return
        source_id = "source:api"
        if await self.sources.get(source_id) is None:
            await self.sources.add(Source(source_id, "api", 0, self.clock.now()))
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

    async def retrieve_for_eval(self, question: str) -> RetrievedEvidence:
        """Retrieve evidence for an explicit internal evaluation."""
        return await self.retrieval.retrieve(clean_question(question), all_scopes=True)

    async def dry_run(self, question: str) -> AnswerPreview:
        """Decide an answer without persisting it."""
        cleaned = clean_question(question)
        retrieved = await self.retrieval.retrieve(cleaned, all_scopes=True)
        return AnswerPreview(await self.decide(cleaned, retrieved), retrieved.all())
