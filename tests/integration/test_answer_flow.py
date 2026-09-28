# SPDX-License-Identifier: MIT
"""Integration tests for durable answer preparation."""

import json
from dataclasses import replace
from datetime import UTC, datetime

from knowledge_bot.application.answer_policy import AnswerPolicy
from knowledge_bot.application.answer_question import ABSTENTION_TEXT, AnswerService
from knowledge_bot.application.retrieval import RetrievalService
from knowledge_bot.domain.enums import AnswerMode, AnswerReason, ContentType
from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)
from knowledge_bot.models.common import SourceDescriptor
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.ports.generator import GenerationOutput
from knowledge_bot.ports.vector_store import VectorRecord
from tests.fakes.ai import (
    FakeEmbedder,
    FakeGenerator,
    FakeVectorStore,
)
from tests.fakes.repositories import InMemoryBotAnswerRepository
from tests.fakes.support import FrozenClock

NOW = datetime(2026, 9, 19, 9, 32, tzinfo=UTC)
SPACE_ID = "sp_" + "1" * 32
SCOPE_KEY = "space:" + SPACE_ID


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        id="m1",
        source=SourceDescriptor(
            id="src:telegram:runtime", kind="telegram", authority=40
        ),
        conversation_id="-100",
        space_id=SPACE_ID,
        sender_is_admin=False,
        timestamp=NOW,
        content_type=ContentType.TEXT,
        source_message_id="m1",
        sender_id="hash",
        text=text,
    )


async def _service(
    records: list[VectorRecord],
    result: GenerationOutput | None = None,
    error: Exception | None = None,
) -> tuple[AnswerService, InMemoryBotAnswerRepository, FakeGenerator]:
    store = FakeVectorStore()
    await store.upsert(records)
    generator = FakeGenerator(result, error)
    answers = InMemoryBotAnswerRepository()
    service = AnswerService(
        retrieval=RetrievalService(
            embedder=FakeEmbedder([1.0, 0.0]),
            vectors=store,
            qa_top_k=5,
            message_top_k=5,
        ),
        generator=generator,
        answers=answers,
        clock=FrozenClock(NOW),
    )
    return service, answers, generator


def _qa_record() -> VectorRecord:
    return VectorRecord(
        id="qa:web-item",
        values=[1.0, 0.0],
        metadata={
            "kind": "qa",
            "object_id": "web-item",
            "version_id": "qav:web-1",
            "canonical_key": "equipment",
            "status": "active",
            "scope_key": "global",
            "text": "Els dimarts.",
            "authority": 90,
            "question": "Quan entrenen?",
        },
    )


def _message_record() -> VectorRecord:
    return VectorRecord(
        id="msg:m9",
        values=[1.0, 0.0],
        metadata={
            "kind": "message_evidence",
            "scope_key": SCOPE_KEY,
            "text": "els dimarts",
            "authority": 40,
        },
    )


async def test_grounded_answer_is_persisted_before_delivery() -> None:
    """Answer preparation stores the rendered response and citations."""
    service, answers, generator = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["qa:web-item"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert response.answer_id == "ans:m1"
    assert response.sources[0].source_id == "qa:web-item"
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert stored.rendered_text == response.rendered_text
    assert json.loads(stored.source_details_json)[0]["source_id"] == "qa:web-item"
    assert len(generator.requests) == 1


async def test_answer_message_replay_returns_exact_response() -> None:
    """A repeated message reuses the durable answer without another decision."""
    service, _, generator = await _service([_qa_record()])
    message = _message("/ask quan entrenen?")
    first = await service.answer_message(message)
    second = await service.answer_message(message)
    assert first == second
    assert len(generator.requests) == 1


async def test_synthesis_uses_generator_and_citations() -> None:
    """Message evidence goes through the generator and validates citations."""
    service, _, generator = await _service(
        [_message_record()],
        GenerationOutput(
            status="answered", answer="Sí, els dimarts.", source_ids=["msg:m9"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert len(generator.requests) == 1
    assert response.sources[0].source_id == "msg:m9"


async def test_no_knowledge_abstains_and_persists() -> None:
    """With an empty index the bot abstains durably."""
    service, answers, generator = await _service([])
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.ABSTENTION
    assert response.answer == ABSTENTION_TEXT
    assert generator.requests == []
    assert await answers.get("ans:m1") is not None


async def test_empty_question_is_not_answered() -> None:
    """A bare ask with no question produces no answer."""
    service, _, _ = await _service([])
    assert await service.answer_message(_message("/ask")) is None


async def _trace(answers: InMemoryBotAnswerRepository) -> dict:
    """Return the stored debug trace of the one persisted answer."""
    stored = await answers.get("ans:m1")
    assert stored is not None
    trace: dict = json.loads(stored.trace_json)
    return trace


async def test_trace_records_why_a_model_refused() -> None:
    """A model refusal is distinguishable from a floor refusal in the trace."""
    service, answers, _ = await _service(
        [_qa_record()], GenerationOutput(status="insufficient")
    )
    await service.answer_message(_message("/ask quan entrenen?"))
    trace = await _trace(answers)
    assert trace["refusal_reason"] == "model_insufficient"
    assert trace["selected"] == ["qa:web-item"]
    assert trace["candidates"] == {
        "qa": [{"id": "qa:web-item", "similarity": 1.0}],
        "message": [],
        # Recorded so a retrieval decision can be attributed after the fact:
        # production tracing is off, so this is the only durable record of
        # which leg surfaced what.
        "qa_lexical": [],
    }
    assert trace["generation"]["status"] == "insufficient"
    assert trace["generation"]["source_ids"] == []
    assert trace["generation"]["prompt_chars"] > 0
    assert "cited" not in trace


async def test_trace_records_an_uncited_source_as_invalid_source_ids() -> None:
    """A cited id outside the evidence is a distinct refusal reason."""
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Divendres.", source_ids=["qa:other"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.ABSTENTION
    trace = await _trace(answers)
    assert trace["refusal_reason"] == "invalid_source_ids"
    assert trace["generation"]["source_ids"] == ["qa:other"]


async def test_model_echoing_a_prefixless_source_id_is_answered() -> None:
    """Dropping the kind prefix must not discard a grounded answer.

    This is the exact shape the model returned in production: evidence ids are
    ``qa:<object id>``, and the model cited ``<object id>``.
    """
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["web-item"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert response.answer == "Els dimarts."
    assert [source.source_id for source in response.sources] == ["qa:web-item"]
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert json.loads(stored.sources_json) == ["qa:web-item"]
    trace = await _trace(answers)
    assert "refusal_reason" not in trace
    assert trace["generation"]["source_ids"] == ["web-item"]
    assert trace["cited"] == ["qa:web-item"]


async def test_model_citing_a_scope_suffixed_evidence_by_bare_id_is_answered() -> None:
    """A space-local evidence id cites cleanly despite its scope suffix.

    A space-local vector id is ``qa:qa-<id>:space:<space>``. The production
    model cited ``qa-<id>`` and the citation resolver could not strip the
    scope segment, so every space-local grounded answer became an abstention.
    The resolver now strips the scope suffix the same way it strips the kind
    prefix.
    """
    scope = f"space:{SPACE_ID}"
    record = VectorRecord(
        id=f"qa:web-item:{scope}",
        values=[1.0, 0.0],
        metadata={
            "kind": "qa",
            "object_id": "web-item",
            "version_id": f"qav:web-1:{scope}",
            "canonical_key": "equipment",
            "status": "active",
            "scope_key": scope,
            "text": "Els dimarts.",
            "authority": 90,
            "question": "Quan entrenen?",
        },
    )
    service, _, _ = await _service(
        [record],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["web-item"]
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))

    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert response.answer == "Els dimarts."
    assert [source.source_id for source in response.sources] == [f"qa:web-item:{scope}"]


async def test_both_spellings_of_one_id_cite_the_item_once() -> None:
    """A model citing one id twice, spelled two ways, yields one source."""
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered",
            answer="Els dimarts.",
            source_ids=["qa:web-item", "web-item"],
        ),
    )
    response = await service.answer_message(_message("/ask quan entrenen?"))
    assert response is not None
    assert response.mode is AnswerMode.SYNTHESIS
    assert len(response.sources) == 1
    stored = await answers.get("ans:m1")
    assert stored is not None
    assert json.loads(stored.sources_json) == ["qa:web-item"]


async def test_trace_records_a_floor_refusal_without_a_model_call() -> None:
    """No evidence above the floor leaves no generation entry at all."""
    service, answers, generator = await _service([], None)
    service = AnswerService(
        retrieval=service.retrieval,
        generator=service.generator,
        answers=service.answers,
        clock=service.clock,
        policy=AnswerPolicy(floor=1.1),
    )
    await service.answer_message(_message("/ask quan entrenen?"))
    trace = await _trace(answers)
    assert generator.requests == []
    assert trace["refusal_reason"] == "no_evidence"
    assert trace["selected"] == []
    assert "generation" not in trace


async def test_trace_records_a_synthesis_without_its_text() -> None:
    """A successful answer is traced by ids, never by its text."""
    service, answers, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["qa:web-item"]
        ),
    )
    await service.answer_message(_message("/ask quan entrenen?"))
    trace = await _trace(answers)
    assert trace["cited"] == ["qa:web-item"]
    assert "refusal_reason" not in trace
    assert "Els dimarts." not in json.dumps(trace)


async def test_every_outcome_carries_exactly_one_reason() -> None:
    """The six semantic reasons are the whole taxonomy, one per outcome."""
    answered = GenerationOutput(
        status="answered", answer="Els dimarts.", source_ids=["qa:web-item"]
    )
    cases = [
        ([_qa_record()], answered, None, AnswerMode.SYNTHESIS, AnswerReason.ANSWERED),
        (
            [],
            GenerationOutput(status="insufficient"),
            None,
            AnswerMode.ABSTENTION,
            AnswerReason.NO_EVIDENCE,
        ),
        (
            [_qa_record()],
            GenerationOutput(status="insufficient"),
            None,
            AnswerMode.ABSTENTION,
            AnswerReason.MODEL_INSUFFICIENT,
        ),
        (
            [_qa_record()],
            None,
            InvalidModelOutputError("no_json"),
            AnswerMode.ABSTENTION,
            AnswerReason.INVALID_MODEL_OUTPUT,
        ),
        (
            [_qa_record()],
            GenerationOutput(
                status="answered", answer="Divendres.", source_ids=["qa:other"]
            ),
            None,
            AnswerMode.ABSTENTION,
            AnswerReason.INVALID_SOURCE_IDS,
        ),
        (
            [_qa_record()],
            None,
            ModelUnavailableError("generation"),
            AnswerMode.UNAVAILABLE,
            AnswerReason.MODEL_UNAVAILABLE,
        ),
    ]
    for records, result, error, mode, reason in cases:
        service, _, _ = await _service(records, result, error)
        preview = await service.dry_run("Quan entrenen?")
        assert preview.outcome.mode is mode, reason
        assert preview.outcome.reason is reason


async def test_provider_failure_is_not_an_abstention() -> None:
    """A provider failure never reaches the user as an abstention."""
    service, _, _ = await _service(
        [_qa_record()], error=ModelUnavailableError("generation")
    )

    preview = await service.dry_run("Quan entrenen?")

    assert preview.outcome.mode is AnswerMode.UNAVAILABLE
    assert preview.outcome.reason is AnswerReason.MODEL_UNAVAILABLE


async def test_unreadable_output_keeps_the_invalid_code_in_the_trace() -> None:
    """The safe parse code is recorded, the raw model output never is."""
    service, answers, _ = await _service(
        [_qa_record()], error=InvalidModelOutputError("schema_validation")
    )

    await service.answer_message(_message("/ask quan entrenen?"))

    trace = await _trace(answers)
    assert trace["refusal_reason"] == "invalid_model_output"
    assert trace["generation"]["invalid_output_code"] == "schema_validation"


class _RecordingTracer:
    """A tracer that records span names and their attributes in order."""

    def __init__(self) -> None:
        """Start with an empty recording."""
        self.spans: list[str] = []
        self.attributes: dict[str, dict[str, object]] = {}
        self._stack: list[str] = []

    def span(self, name: str, **fields: object) -> "_RecordingSpan":
        """Open a recorded span.

        Args:
            name: The span name.
            **fields: The attributes recorded at open time.

        Returns:
            The span context manager.
        """
        self.spans.append(name)
        self.attributes.setdefault(name, {}).update(fields)
        return _RecordingSpan(self, name)


class _RecordingSpan:
    """One recorded span."""

    def __init__(self, tracer: _RecordingTracer, name: str) -> None:
        """Store the tracer and this span's name.

        Args:
            tracer: The recording tracer.
            name: The span name.
        """
        self._tracer = tracer
        self._name = name

    def __enter__(self) -> "_RecordingSpan":
        """Enter the span.

        Returns:
            The span itself.
        """
        self._tracer._stack.append(self._name)
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Leave the span.

        Args:
            *exc_info: The exception triple, if any.
        """
        self._tracer._stack.pop()

    def set_attributes(self, fields: dict[str, object]) -> None:
        """Record attributes discovered while the span was open.

        Args:
            fields: The attributes.
        """
        self._tracer.attributes.setdefault(self._name, {}).update(fields)


async def test_a_answered_question_produces_the_documented_span_tree() -> None:
    """One question yields the five documented spans, in order."""
    tracer = _RecordingTracer()
    service, _, _ = await _service(
        [_qa_record()],
        GenerationOutput(
            status="answered", answer="Els dimarts.", source_ids=["qa:web-item"]
        ),
    )
    service = replace(service, tracer=tracer)

    await service.dry_run("Quan entrenen?")

    assert tracer.spans == [
        "answer_question",
        "retrieval",
        "evidence_selection",
        "generation",
        "answer_decision",
    ]
    assert tracer.attributes["answer_question"]["reason"] == "answered"
    assert tracer.attributes["answer_question"]["mode"] == "synthesis"
    assert tracer.attributes["evidence_selection"]["selected"] == ["qa:web-item"]
    assert tracer.attributes["generation"]["evidence_count"] == 1
    assert tracer.attributes["answer_decision"]["cited_source_ids"] == ["qa:web-item"]


async def test_an_abstention_produces_the_same_tree_and_says_why() -> None:
    """The tree does not change shape when the answer is refused."""
    tracer = _RecordingTracer()
    service, _, _ = await _service(
        [_qa_record()], GenerationOutput(status="insufficient")
    )
    service = replace(service, tracer=tracer)

    await service.dry_run("Quan entrenen?")

    assert tracer.spans == [
        "answer_question",
        "retrieval",
        "evidence_selection",
        "generation",
        "answer_decision",
    ]
    assert tracer.attributes["answer_question"]["reason"] == "model_insufficient"
    assert tracer.attributes["generation"]["status"] == "insufficient"


async def test_frozen_evidence_bypasses_retrieval_but_not_the_decision_path() -> None:
    """Frozen evidence reaches the same decide(), without the index."""
    tracer = _RecordingTracer()
    service, _, _ = await _service([], error=ModelUnavailableError("embedding"))
    service = replace(service, tracer=tracer)

    evidence = service.frozen_evidence(
        [
            {
                "source_id": "qa-frozen",
                "text": "La botiga obre de 10:00 a 20:00.",
                "authority": 90,
                "label": "Q&A",
                "kind": "qa",
            }
        ]
    )

    await service.dry_run("Quand ouvre la boutique ?", evidence=evidence)

    assert [item.source_id for item in evidence] == ["qa-frozen"]
    assert all(item.similarity >= service.policy.floor for item in evidence)
    assert "retrieval" not in tracer.spans
    assert tracer.spans == [
        "answer_question",
        "evidence_selection",
        "generation",
    ]


async def test_frozen_evidence_cannot_be_filtered_out_by_the_floor() -> None:
    """Selection sees frozen evidence, so a case is never silently no_evidence."""
    service, _, _ = await _service([])

    preview = await service.dry_run(
        "Quan obre la botiga?",
        evidence=service.frozen_evidence(
            [{"source_id": "qa-x", "text": "Obre a les 10:00.", "authority": 90}]
        ),
    )

    assert preview.outcome.reason is not AnswerReason.NO_EVIDENCE
    assert preview.evidence[0].source_id == "qa-x"
