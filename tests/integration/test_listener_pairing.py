# SPDX-License-Identifier: MIT
"""Integration coverage for listener pairing, baseline and model-decided."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from knowledge_bot.application.assessment import SystemOneAssessmentModel
from knowledge_bot.application.classifier import (
    Classification,
    IntentScores,
    prefilter_classification,
)
from knowledge_bot.domain.enums import (
    ClassificationStatus,
    ContentType,
    IndexStatus,
    IntentLabel,
)
from knowledge_bot.domain.errors import InvalidModelOutputError
from knowledge_bot.models.assessment import (
    CandidateRelation,
    MessageAssessment,
    RetroevalCandidate,
)
from knowledge_bot.models.common import SourceDescriptor
from knowledge_bot.models.messages import NormalizedMessage
from tests.fakes.ai import FakeEmbedder
from tests.fakes.context import build_test_context
from tests.fakes.support import FrozenClock

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

#: One unit vector per label, matching the fake linear head.
QUESTION_VECTOR = [1.0, 0.0, 0.0, 0.0]
UPDATE_VECTOR = [0.0, 1.0, 0.0, 0.0]
CORRECTION_VECTOR = [0.0, 0.0, 1.0, 0.0]

LABEL_EMBEDDER = FakeEmbedder(
    vector=QUESTION_VECTOR,
    by_text={
        "Quan entrenem?": QUESTION_VECTOR,
        "Pregunta 1?": QUESTION_VECTOR,
        "Pregunta 2?": QUESTION_VECTOR,
        "Diumenge?": QUESTION_VECTOR,
        "A les sis.": UPDATE_VECTOR,
        "Al dilluns.": UPDATE_VECTOR,
        "Era dimarts.": CORRECTION_VECTOR,
    },
)


class FakeSystemOneTransport:
    """A decision service that answers from a fixed script and counts calls.

    A question mark in the state makes the message a question, so the same
    script can drive a whole conversation. Relevance is decided per candidate
    question text, which keeps assertions readable while the candidate ids stay
    opaque hashes.
    """

    def __init__(self, *, high: str | None = None, low: float = 0.60) -> None:
        """Configure which candidate question scores high, and the rest."""
        self.high = high
        self.low = low
        self.texts: list[str] = []
        self.question_counts: list[int] = []

    async def decide(
        self,
        *,
        model: str,
        state: object,
        questions: dict[str, object],
    ) -> dict[str, object]:
        """Answer the intent and every candidate relevance in one call."""
        del model
        payload = cast(dict[str, object], state)
        text = str(payload["current_message"])
        candidates = cast(list[dict[str, str]], payload["candidate_questions"])
        self.texts.append(text)
        self.question_counts.append(len(questions))
        label = (
            IntentLabel.QUESTION.value
            if text.endswith("?")
            else IntentLabel.KNOWLEDGE_UPDATE.value
        )
        probabilities = {item.value: 0.02 for item in IntentLabel}
        probabilities[label] = 0.94
        best = max(probabilities, key=lambda key: probabilities[key])
        answers: dict[str, object] = {
            "intent": {
                "type": "choice",
                "choice": best,
                "probabilities": probabilities,
            }
        }
        for candidate in candidates:
            name = f"pair.{candidate['id']}.relevant"
            relevant = candidate["question"] == self.high
            answers[name] = {
                "type": "noul",
                "noul": 0.95 if relevant else self.low,
            }
        return {"answers": answers}


def _message(
    message_id: str, text: str, timestamp: datetime = NOW
) -> NormalizedMessage:
    """Build one unaddressed group message."""
    return NormalizedMessage(
        id=message_id,
        source=SourceDescriptor(id="listener-source", kind="test", authority=40),
        conversation_id="conversation-1",
        sender_is_admin=False,
        timestamp=timestamp,
        content_type=ContentType.TEXT,
        text=text,
    )


def _reply(message_id: str, text: str, parent: str) -> NormalizedMessage:
    """Build a message that replies to a parent message."""
    message = _message(message_id, text)
    return message.model_copy(update={"reply_to_message_id": parent})


def _candidate(
    candidate_id: str, question: str, relation: CandidateRelation
) -> RetroevalCandidate:
    """Return one candidate question."""
    return RetroevalCandidate(
        candidate_id=candidate_id,
        question_message_id=f"stored-{candidate_id}",
        question=question,
        relation=relation,
    )


def _answer_like() -> Classification:
    """Return a confident knowledge update."""
    return Classification(
        scores=IntentScores(knowledge_update=0.94),
        best_label=IntentLabel.KNOWLEDGE_UPDATE,
        best_score=0.94,
        margin=0.92,
        embedding=(),
    )


def test_temporal_pair_becomes_normal_message_evidence() -> None:
    """A confident answer pairs with the single recent confident question."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        first = await context.listener.handle(_message("q1", "Quan entrenem?"))
        assert first.status == "ingest"
        assert first.confident_question
        result = await context.listener.handle(_message("a1", "A les sis."))
        assert result.status == "ingest_pair"
        matches = await context.answer.retrieval.vectors.query(
            [1.0, 0.0], top_k=5, filters={"kind": "message_evidence"}
        )
        assert len(matches) == 1
        assert matches[0].id == "msg:a1"
        assert matches[0].metadata["question"] == "Quan entrenem?"

    asyncio.run(run())


def test_temporal_pairing_is_idempotent() -> None:
    """Re-processing the same answer does not duplicate the pair."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("q1", "Quan entrenem?"))
        await context.listener.handle(_message("a1", "A les sis."))
        await context.listener.handle(_message("a1", "A les sis."))
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question == "Quan entrenem?"
        matches = await context.answer.retrieval.vectors.query(
            [1.0, 0.0], top_k=5, filters={"kind": "message_evidence"}
        )
        assert len(matches) == 1

    asyncio.run(run())


def test_two_recent_questions_are_ambiguous_and_not_paired() -> None:
    """With more than one plausible question, the baseline refuses to pair."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("q1", "Pregunta 1?"))
        await context.listener.handle(_message("q2", "Pregunta 2?"))
        result = await context.listener.handle(_message("a1", "A les sis."))
        assert result.status == "ingest"
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question is None

    asyncio.run(run())


def test_explicit_reply_pairs_with_its_parent() -> None:
    """An explicit reply pairs with the parent even beside another question."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("conversation-1:10", "Quan entrenem?"))
        await context.listener.handle(_message("q2", "Pregunta 2?"))
        result = await context.listener.handle(_reply("a1", "A les sis.", parent="10"))
        assert result.status == "ingest_pair"
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question == "Quan entrenem?"

    asyncio.run(run())


def test_ambiguous_question_is_never_a_pending_candidate() -> None:
    """A question without margin does not attract a later pairing."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.ingestor.ingest(
            _message("q1", "Diumenge?"),
            intent_label="question",
            intent_score=0.55,
            intent_scores={"question": 0.55, "margin": 0.05},
            classification_status=ClassificationStatus.CLASSIFIED,
        )
        result = await context.listener.handle(_message("a1", "A les sis."))
        assert result.status == "ingest"
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question is None

    asyncio.run(run())


def test_old_questions_leave_the_pairing_window() -> None:
    """A question older than the window never pairs."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        stale = NOW - timedelta(minutes=10)
        context.clock.advance_to(stale)
        await context.listener.handle(_message("q1", "Quan entrenem?", stale))
        context.clock.advance_to(NOW)
        result = await context.listener.handle(_message("a1", "A les sis."))
        assert result.status == "ingest"

    asyncio.run(run())


def test_chitchat_is_stored_without_pairing() -> None:
    """Clear-cut chitchat is stored unclassified and never indexed."""
    context, _ = build_test_context(embedder=LABEL_EMBEDDER)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("q1", "Quan entrenem?"))
        await context.listener.handle(_message("c1", "gràcies"))
        stored = await context.ingestor.get_message("c1")
        assert stored is not None
        assert stored.classification_status == (ClassificationStatus.PREFILTER_CHITCHAT)
        assert stored.context_question is None
        assert stored.index_status == IndexStatus.NOT_ELIGIBLE

    asyncio.run(run())


def test_one_request_carries_intent_and_every_candidate() -> None:
    """One message costs one request, whatever the candidate count."""
    transport = FakeSystemOneTransport(high="Pregunta 1?")
    model = SystemOneAssessmentModel(
        transport=transport, model="tev1:0.8b", include_relevance=True
    )
    context, _ = build_test_context(embedder=LABEL_EMBEDDER, assessment=model)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("q1", "Pregunta 1?"))
        await context.listener.handle(_message("q2", "Pregunta 2?"))
        result = await context.listener.handle(_message("a1", "A les sis."))
        answer_calls = [
            index for index, text in enumerate(transport.texts) if text == "A les sis."
        ]
        assert len(answer_calls) == 1
        assert transport.question_counts[answer_calls[0]] == 3
        assert result.status == "ingest_pair"
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question == "Pregunta 1?"

    asyncio.run(run())


def test_relevance_off_keeps_one_question_and_deterministic_pairing() -> None:
    """The safety floor: the model classifies, the policy still pairs."""
    transport = FakeSystemOneTransport()
    model = SystemOneAssessmentModel(transport=transport, model="tev1:0.8b")
    context, _ = build_test_context(embedder=LABEL_EMBEDDER, assessment=model)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("q1", "Quan entrenem?"))
        result = await context.listener.handle(_message("a1", "A les sis."))
        assert transport.question_counts == [1, 1]
        assert result.status == "ingest_pair"
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question == "Quan entrenem?"

    asyncio.run(run())


def test_low_relevance_everywhere_pairs_nothing() -> None:
    """Every candidate below the threshold means no pair at all."""
    transport = FakeSystemOneTransport(low=0.10)
    model = SystemOneAssessmentModel(
        transport=transport, model="tev1:0.8b", include_relevance=True
    )
    context, _ = build_test_context(embedder=LABEL_EMBEDDER, assessment=model)

    async def run() -> None:
        assert isinstance(context.clock, FrozenClock)
        context.clock.advance_to(NOW)
        await context.listener.handle(_message("q1", "Pregunta 1?"))
        await context.listener.handle(_message("q2", "Pregunta 2?"))
        result = await context.listener.handle(_message("a1", "A les sis."))
        assert result.status == "ingest"
        stored = await context.ingestor.get_message("a1")
        assert stored is not None
        assert stored.context_question is None

    asyncio.run(run())


def test_select_pair_prefers_a_decisive_top_candidate() -> None:
    """Relevance decides when the top score clears threshold and margin."""
    context, _ = build_test_context()
    candidates = (
        _candidate("c1", "Quan entrenem?", "explicit_reply"),
        _candidate("c2", "Què fem?", "temporal_window"),
    )
    decisive = MessageAssessment(
        classification=_answer_like(),
        pair_relevance={"c1": 0.93, "c2": 0.70},
    )
    chosen = context.pairing.select_pair(_message("a1", "x"), decisive, candidates)
    assert chosen is candidates[0]
    tied = MessageAssessment(
        classification=_answer_like(),
        pair_relevance={"c1": 0.93, "c2": 0.85},
    )
    assert context.pairing.select_pair(_message("a1", "x"), tied, candidates) is None


def test_select_pair_refuses_a_question_or_chitchat() -> None:
    """Relevance never turns a question or chitchat into an answer."""
    context, _ = build_test_context()
    candidates = (_candidate("c1", "Quan entrenem?", "temporal_window"),)
    question = MessageAssessment(
        classification=Classification(
            scores=IntentScores(question=0.95),
            best_label=IntentLabel.QUESTION,
            best_score=0.95,
            margin=0.90,
            embedding=(),
        ),
        pair_relevance={"c1": 0.99},
    )
    message = _message("q1", "x")
    assert context.pairing.select_pair(message, question, candidates) is None
    chitchat = MessageAssessment(
        classification=prefilter_classification(), pair_relevance={"c1": 0.99}
    )
    assert context.pairing.select_pair(message, chitchat, candidates) is None


def test_malformed_decision_output_is_an_error() -> None:
    """A missing candidate answer never becomes a silent zero."""

    class BrokenTransport:
        async def decide(
            self,
            *,
            model: str,
            state: object,
            questions: dict[str, object],
        ) -> dict[str, object]:
            del model, state, questions
            return {"answers": {}}

    model = SystemOneAssessmentModel(transport=BrokenTransport(), model="tev1:0.8b")
    candidates = (_candidate("c1", "Quan entrenem?", "temporal_window"),)

    async def run() -> None:
        with pytest.raises(InvalidModelOutputError):
            await model.assess("A les sis.", candidates=candidates)

    asyncio.run(run())
