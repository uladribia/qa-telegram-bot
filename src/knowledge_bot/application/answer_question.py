# SPDX-License-Identifier: MIT
"""Prepare and persist grounded answers; channel adapters deliver them.

Every attempt persists a ``trace_json`` debug trace next to the answer: the
floor, the retrieved candidates with their similarities, the ids the floor
selected, the generation sizes and returned ids, the timings, and one
``refusal_reason`` saying which branch refused. The worker keeps no log
history, so this column is the only durable record of why a question was
answered or refused. It deliberately holds no text: the prompt, the evidence
bodies and the model output are reconstructible from the ids it stores, and
stay out of storage per the logging rules.
"""

import json
import time
from dataclasses import dataclass, field

from knowledge_bot.application.answer_policy import AnswerPolicy
from knowledge_bot.application.retrieval import (
    Evidence,
    RetrievalService,
    RetrievedEvidence,
)
from knowledge_bot.domain.entities import BotAnswer, Conversation, Source
from knowledge_bot.domain.enums import AnswerMode, AnswerReason
from knowledge_bot.domain.errors import InvalidModelOutputError, ModelUnavailableError
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.models.questions import (
    AnswerSource,
    AskQuestionRequest,
    AskQuestionResponse,
)
from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest, Generator
from knowledge_bot.ports.repositories import (
    BotAnswerRepository,
    ConversationRepository,
    SourceRepository,
)
from knowledge_bot.ports.telemetry import NoopTracer, Tracer

ABSTENTION_TEXT = "No tinc prou informació fiable per respondre-ho."
UNAVAILABLE_TEXT = (
    "Ara mateix no puc consultar la informació. Torna-ho a provar en una estona."
)

#: Similarity given to frozen evaluation evidence, high enough that selection
#: can never reject it for being below the floor.
FROZEN_SIMILARITY = 1.0


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
    """The decided answer, mode, reason, source ids, and rendered text."""

    answer: str
    mode: AnswerMode
    reason: AnswerReason
    source_ids: list[str]
    text: str
    qa_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class AnswerPreview:
    """A decided answer and its supporting evidence."""

    outcome: AnswerOutcome
    evidence: list[Evidence]
    trace: dict[str, object] = field(default_factory=dict)


def _candidates(items: list[Evidence]) -> list[dict[str, object]]:
    """Describe retrieved candidates by id and similarity, never by text."""
    return [
        {"id": item.source_id, "similarity": round(item.similarity, 4)}
        for item in items
    ]


def _elapsed(started: float) -> float:
    """Return the milliseconds since ``started``."""
    return round((time.perf_counter() - started) * 1000, 2)


def _object_id(source_id: str) -> str:
    """Return the object id behind a ``kind:``-namespaced vector id.

    Evidence ids reach the model as ``qa:qa-<id>`` / ``msg-<id>``; it commonly
    echoes the object id alone, dropping the kind prefix. Citation matching
    resolves both spellings instead of discarding the answer over the prefix.
    """
    return source_id.split(":", 1)[-1]


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


def _abstain(reason: AnswerReason) -> AnswerOutcome:
    """Return the deterministic abstention outcome for one reason."""
    return AnswerOutcome(
        ABSTENTION_TEXT, AnswerMode.ABSTENTION, reason, [], ABSTENTION_TEXT
    )


@dataclass(frozen=True, slots=True)
class AnswerService:
    """Decide and persist answers without performing channel delivery."""

    retrieval: RetrievalService
    generator: Generator
    answers: BotAnswerRepository
    clock: Clock
    policy: AnswerPolicy = field(default_factory=AnswerPolicy)
    conversations: ConversationRepository | None = None
    sources: SourceRepository | None = None
    tracer: Tracer = field(default_factory=NoopTracer)

    async def get_answer(self, answer_id: str) -> BotAnswer | None:
        """Return a stored answer."""
        return await self.answers.get(answer_id)

    async def decide(
        self,
        question: str,
        retrieved: RetrievedEvidence,
        trace: dict[str, object] | None = None,
    ) -> AnswerOutcome:
        """Answer from the selected evidence, or abstain without it.

        Every answered question goes to the generator: a verbatim echo cannot
        decline, and the model returns ``insufficient`` for unknown questions
        instead of quoting an unrelated document.

        Args:
            question: The user question.
            retrieved: The candidates retrieval returned.
            trace: Optional debug trace to record the decision in.

        Returns:
            The answer, abstention, or unavailable outcome, with exactly one
            ``AnswerReason``.

        Raises:
            Nothing: both provider failures and unreadable output are mapped
                to safe user-facing outcomes here, so every caller sees the
                same decision with the same diagnostic reason.
        """
        selection = self.policy.select(retrieved.qa, retrieved.messages)
        with self.tracer.span(
            "evidence_selection",
            floor=self.policy.floor,
            qa_candidates=len(retrieved.qa),
            message_candidates=len(retrieved.messages),
        ) as selection_span:
            selection_span.set_attributes(
                {
                    "selected": [item.source_id for item in selection.evidence],
                    "selected_count": len(selection.evidence),
                    "abstained": selection.abstain,
                }
            )
            if trace is not None:
                trace["selected"] = [item.source_id for item in selection.evidence]
            if selection.abstain:
                return self._refuse(AnswerReason.NO_EVIDENCE, trace)
        evidence = list(selection.evidence)
        started = time.perf_counter()
        with self.tracer.span(
            "generation",
            evidence_ids=[item.source_id for item in evidence],
            evidence_count=len(evidence),
            evidence_authorities=[item.authority for item in evidence],
            prompt_chars=len(question) + sum(len(item.text) for item in evidence),
        ) as generation_span:
            try:
                result = await self.generator.generate(
                    GenerationRequest(
                        question=question,
                        evidence=[
                            EvidenceItem(
                                source_id=item.source_id,
                                text=item.text,
                                label=item.label,
                                authority=item.authority,
                                similarity=item.similarity,
                                provenance=item.provenance,
                                source_kind=item.source_kind,
                                question=item.question,
                            )
                            for item in evidence
                        ],
                    )
                )
            except ModelUnavailableError:
                generation_span.set_attributes(
                    {"status": "unavailable", "duration_ms": _elapsed(started)}
                )
                return self._unavailable(trace)
            except InvalidModelOutputError as error:
                generation_span.set_attributes(
                    {
                        "status": "invalid_output",
                        "invalid_output_code": error.code,
                        "duration_ms": _elapsed(started),
                    }
                )
                if trace is not None:
                    trace["generation"] = {
                        "status": "invalid_output",
                        "invalid_output_code": error.code,
                        "duration_ms": _elapsed(started),
                    }
                return self._refuse(AnswerReason.INVALID_MODEL_OUTPUT, trace)
            generation_span.set_attributes(
                {
                    "status": result.status,
                    "response_chars": len(result.answer),
                    "model_source_ids": list(result.source_ids),
                    "duration_ms": _elapsed(started),
                }
            )
        resolved = self._resolve(evidence, result.source_ids)
        if trace is not None:
            trace["generation"] = {
                "prompt_chars": len(question)
                + sum(len(item.text) for item in evidence),
                "response_chars": len(result.answer),
                "status": result.status,
                "source_ids": list(result.source_ids),
                "duration_ms": _elapsed(started),
            }
        with self.tracer.span("answer_decision") as decision_span:
            if result.status != "answered" or not result.answer.strip():
                decision_span.set_attributes(
                    {
                        "mode": "abstention",
                        "reason": AnswerReason.MODEL_INSUFFICIENT.value,
                    }
                )
                return self._refuse(AnswerReason.MODEL_INSUFFICIENT, trace)
            if any(item is None for item in resolved):
                decision_span.set_attributes(
                    {
                        "mode": "abstention",
                        "reason": AnswerReason.INVALID_SOURCE_IDS.value,
                    }
                )
                return self._refuse(AnswerReason.INVALID_SOURCE_IDS, trace)
            cited = [item for item in resolved if item is not None]
            if trace is not None:
                trace["cited"] = [item.source_id for item in cited]
            decision_span.set_attributes(
                {
                    "mode": "synthesis",
                    "reason": AnswerReason.ANSWERED.value,
                    "cited_source_ids": [item.source_id for item in cited],
                }
            )
            return AnswerOutcome(
                answer=result.answer,
                mode=AnswerMode.SYNTHESIS,
                reason=AnswerReason.ANSWERED,
                source_ids=[item.source_id for item in cited],
                text=_render(result.answer, cited),
            )

    async def _decide_retrieved(
        self,
        question: str,
        space_id: str | None,
        trace: dict[str, object],
        started: float,
    ) -> AnswerPreview:
        """Retrieve and decide, with the retrieval span around retrieval.

        Args:
            question: The cleaned question.
            space_id: The logical space to search, or ``None`` for global.
            trace: The debug trace to fill.
            started: When the question started, for the retrieval timing.

        Returns:
            The preview, including the unavailable outcome on a provider
            failure, which is a decision like any other.
        """
        retrieved = await self._retrieve(question, space_id, trace, started)
        if retrieved is None:
            return AnswerPreview(self._unavailable(trace), [], trace)
        return AnswerPreview(
            await self.decide(question, retrieved, trace), retrieved.all(), trace
        )

    @staticmethod
    def _unavailable(trace: dict[str, object] | None) -> AnswerOutcome:
        """Return the unavailable outcome for a provider failure.

        A failed embedding or generation call is the same user-visible event:
        the bot cannot consult its information right now. Recording the reason
        keeps it distinct from a model that declined.

        Args:
            trace: Optional debug trace.

        Returns:
            The unavailable outcome.
        """
        if trace is not None:
            trace["refusal_reason"] = AnswerReason.MODEL_UNAVAILABLE.value
        return AnswerOutcome(
            UNAVAILABLE_TEXT,
            AnswerMode.UNAVAILABLE,
            AnswerReason.MODEL_UNAVAILABLE,
            [],
            UNAVAILABLE_TEXT,
        )

    @staticmethod
    def _refuse(reason: AnswerReason, trace: dict[str, object] | None) -> AnswerOutcome:
        """Abstain for one reason and record it in the trace.

        Args:
            reason: Why the answer is an abstention.
            trace: Optional debug trace.

        Returns:
            The abstention outcome.
        """
        if trace is not None:
            trace["refusal_reason"] = reason.value
        return _abstain(reason)

    @staticmethod
    def _resolve(
        evidence: list[Evidence], source_ids: list[str]
    ) -> list[Evidence | None]:
        """Match the model's returned ids to evidence, in canonical form.

        Both the exact vector id and the bare object id resolve to the same
        evidence item; an id matching neither stays ``None`` so the caller can
        refuse, while a repeated citation is dropped rather than refused. Exact
        spellings are registered first, so a bare id can never shadow a real one.
        """
        by_id: dict[str, Evidence] = {item.source_id: item for item in evidence}
        for item in evidence:
            by_id.setdefault(_object_id(item.source_id), item)
        cited: list[Evidence | None] = []
        seen: set[str] = set()
        for source_id in source_ids:
            item = by_id.get(source_id) or by_id.get(_object_id(source_id))
            if item is None:
                cited.append(None)
            elif item.source_id not in seen:
                seen.add(item.source_id)
                cited.append(item)
        return cited

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
        started = time.perf_counter()
        trace: dict[str, object] = {"floor": self.policy.floor}
        with self.tracer.span(
            "answer_question",
            answer_id=answer_id,
            conversation_id=conversation_id,
            space_id=space_id,
            question_chars=len(question),
        ) as question_span:
            preview = await self._decide_retrieved(question, space_id, trace, started)
            trace["total_ms"] = _elapsed(started)
            question_span.set_attributes(
                {
                    "mode": preview.outcome.mode.value,
                    "reason": preview.outcome.reason.value,
                    "duration_ms": trace["total_ms"],
                }
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
            trace_json=json.dumps(preview.trace, separators=(",", ":")),
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
        """Retrieve evidence for an explicit internal evaluation.

        Evaluation uses the same scope semantics as a normal global question:
        a case that needs a specific space passes one through the eval request
        instead of widening the search to every group.
        """
        return await self.retrieval.retrieve(clean_question(question))

    async def dry_run(
        self, question: str, *, evidence: list[Evidence] | None = None
    ) -> AnswerPreview:
        """Decide an answer without persisting it, with the same trace shape.

        The evaluation path opens the same spans as a real answer, so a failed
        evaluation case is attributable from telemetry the same way a
        user-visible abstention is.

        Args:
            question: The question to decide.
            evidence: Frozen evidence for a generator-only evaluation. When
                given, retrieval is bypassed and this exact set is selected, so
                the case measures the generator and not the index. The path
                through ``decide`` is the production one; only the candidate
                source differs.

        Returns:
            The preview.
        """
        cleaned = clean_question(question)
        started = time.perf_counter()
        trace: dict[str, object] = {"floor": self.policy.floor}
        with self.tracer.span(
            "answer_question",
            answer_id=None,
            question_chars=len(cleaned),
            evaluation=True,
            frozen_evidence=evidence is not None,
        ) as question_span:
            retrieved = (
                RetrievedEvidence(qa=list(evidence))
                if evidence is not None
                else await self._retrieve(cleaned, None, trace, started)
            )
            preview = (
                AnswerPreview(self._unavailable(trace), [], trace)
                if retrieved is None
                else AnswerPreview(
                    await self.decide(cleaned, retrieved, trace),
                    retrieved.all(),
                    trace,
                )
            )
            question_span.set_attributes(
                {
                    "mode": preview.outcome.mode.value,
                    "reason": preview.outcome.reason.value,
                    "duration_ms": _elapsed(started),
                }
            )
            return preview

    async def _retrieve(
        self,
        question: str,
        space_id: str | None,
        trace: dict[str, object],
        started: float,
    ) -> RetrievedEvidence | None:
        """Retrieve candidates, or ``None`` when the provider is unavailable.

        Args:
            question: The cleaned question.
            space_id: The logical space to search, or ``None`` for global.
            trace: The debug trace to fill.
            started: When the question started, for the retrieval timing.

        Returns:
            The candidates, or ``None`` on a provider failure, which the caller
            turns into the unavailable outcome.
        """
        try:
            with self.tracer.span("retrieval", space_id=space_id) as retrieval_span:
                retrieved = await self.retrieval.retrieve(question, space_id)
                retrieval_span.set_attributes(
                    {
                        "qa_candidates": len(retrieved.qa),
                        "message_candidates": len(retrieved.messages),
                        "qa_similarities": [
                            round(item.similarity, 4) for item in retrieved.qa
                        ],
                        "message_similarities": [
                            round(item.similarity, 4) for item in retrieved.messages
                        ],
                    }
                )
        except ModelUnavailableError:
            return None
        trace["retrieval_ms"] = _elapsed(started)
        trace["candidates"] = {
            "qa": _candidates(retrieved.qa),
            "message": _candidates(retrieved.messages),
            # Recorded separately because production tracing is off, so
            # trace_json is the only durable record of which leg surfaced what.
            "qa_lexical": _candidates(retrieved.lexical_qa),
        }
        return retrieved

    def frozen_evidence(self, evidence: list[dict[str, object]]) -> list[Evidence]:
        """Build evaluation evidence that clears the selection floor.

        Args:
            evidence: The frozen cases, each with ``source_id``, ``text``, and
                optionally ``label``, ``authority``, and ``kind``.

        Returns:
            The evidence, with a similarity that unambiguously survives the
            deterministic floor.
        """
        frozen: list[dict[str, object]] = evidence
        return [
            Evidence(
                source_id=str(item.get("source_id", "")),
                label=str(item.get("label", "Q&A")),
                text=str(item.get("text", "")),
                authority=int(str(item.get("authority", 50))),
                similarity=FROZEN_SIMILARITY,
                qa_version_id=(
                    f"frozen:{item.get('source_id')}"
                    if str(item.get("kind", "qa")) == "qa"
                    else None
                ),
            )
            for item in frozen
        ]
