# SPDX-License-Identifier: MIT
"""The answer-path decisions: sufficiency, selection, and failing closed."""

import asyncio
import logging

import pytest

from knowledge_bot.application.answer_decisions import (
    AnswerDecisionGate,
    AnswerDecisionSettings,
)
from knowledge_bot.application.answer_policy import AnswerPolicy
from knowledge_bot.application.assessment import (
    EvidenceDecision,
    SystemOneAnswerDecisionModel,
)
from knowledge_bot.application.retrieval import Evidence
from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)
from knowledge_bot.infrastructure.cloudflare.system_one import (
    WorkersAISystemOneTransport,
)
from knowledge_bot.ports.system_one import SystemOneTransport

pytestmark = pytest.mark.unit


def _evidence(source_id: str, text: str, similarity: float) -> Evidence:
    """Build one retrieved evidence item."""
    return Evidence(
        source_id=source_id,
        label="Q&A",
        text=text,
        authority=50,
        similarity=similarity,
    )


RIGHT = _evidence("qa-right", "Els entrenaments són dimarts.", 0.9)
OTHER = _evidence("qa-other", "El bar obre a les nou.", 0.4)


class ScriptedTransport(SystemOneTransport):
    """Marker: the transport satisfies the port the gate depends on."""

    """A decision transport that answers from a script and counts requests."""

    def __init__(self, sufficiency: float, relevance: dict[str, float]) -> None:
        """Configure the answers every call returns."""
        self.sufficiency = sufficiency
        self.relevance = relevance
        self.calls: list[dict[str, object]] = []
        self.question_names: list[set[str]] = []

    async def decide(
        self, *, model: str, state: object, questions: dict[str, object]
    ) -> dict[str, object]:
        """Answer sufficiency and per-item relevance in one request."""
        self.calls.append({"model": model, "state": state, "questions": questions})
        self.question_names.append(set(questions))
        answers: dict[str, object] = {
            "evidence.sufficient": {"type": "noul", "noul": self.sufficiency}
        }
        for name in questions:
            if name == "evidence.sufficient":
                continue
            evidence_id = name.removeprefix("evidence.").removesuffix(".relevant")
            answers[name] = {
                "type": "noul",
                "noul": self.relevance.get(evidence_id, 0.0),
            }
        return {"answers": answers}


class BrokenTransport:
    """A decision transport that is not answering."""

    def __init__(self, error: Exception) -> None:
        """Configure the failure."""
        self.error = error

    async def decide(
        self, *, model: str, state: object, questions: dict[str, object]
    ) -> dict[str, object]:
        """Fail like an unreachable decision service."""
        del model, state, questions
        raise self.error


def _gate(transport: SystemOneTransport, **settings: float) -> AnswerDecisionGate:
    """Build the gate over one transport."""
    return AnswerDecisionGate(
        policy=AnswerPolicy(floor=0.35),
        model=SystemOneAnswerDecisionModel(
            transport=transport,
            model="@cf/cloudflare/clef-flash",
        ),
        settings=AnswerDecisionSettings(**settings),
    )


@pytest.mark.asyncio
async def test_one_request_carries_both_decisions() -> None:
    """Sufficiency and per-item relevance travel together, once."""
    transport = ScriptedTransport(0.95, {"qa-right": 0.97, "qa-other": 0.03})
    gate = _gate(transport)

    selection, reason = await gate.select("Quan entrenen?", [RIGHT, OTHER], [])

    assert reason == "decision"
    assert selection.abstain is False
    assert [item.source_id for item in selection.evidence] == ["qa-right"]
    assert len(transport.calls) == 1
    assert transport.question_names[0] == {
        "evidence.sufficient",
        "evidence.qa-right.relevant",
        "evidence.qa-other.relevant",
    }


@pytest.mark.asyncio
async def test_insufficient_evidence_abstains() -> None:
    """A shortlist nothing answers is an abstention, not a thin answer."""
    transport = ScriptedTransport(0.10, {"qa-right": 0.05, "qa-other": 0.02})
    gate = _gate(transport)

    selection, reason = await gate.select("Quan entrenen?", [RIGHT, OTHER], [])

    assert selection.abstain is True
    assert selection.evidence == ()
    assert reason == "decision"


@pytest.mark.asyncio
async def test_items_below_the_selection_threshold_are_dropped() -> None:
    """Enough evidence, but only some items answer: the rest do not travel."""
    transport = ScriptedTransport(0.95, {"qa-right": 0.95, "qa-other": 0.40})
    gate = _gate(transport)

    selection, _ = await gate.select("Quan entrenen?", [RIGHT, OTHER], [])

    assert [item.source_id for item in selection.evidence] == ["qa-right"]


@pytest.mark.asyncio
async def test_a_kept_nothing_is_an_abstention() -> None:
    """Saying it is enough and keeping nothing cannot become an empty answer."""
    transport = ScriptedTransport(0.99, {"qa-right": 0.10, "qa-other": 0.10})
    gate = _gate(transport)

    selection, _ = await gate.select("Quan entrenen?", [RIGHT, OTHER], [])

    assert selection.abstain is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (ModelUnavailableError("decision"), InvalidModelOutputError("schema_validation")),
)
async def test_an_unusable_decision_degrades_to_the_floor(error: Exception) -> None:
    """A decision service failure must not lose the question or invent a reason."""
    gate = _gate(BrokenTransport(error))

    selection, reason = await gate.select("Quan entrenen?", [RIGHT, OTHER], [])

    assert reason == "floor"
    assert selection.abstain is False
    assert [item.source_id for item in selection.evidence] == ["qa-right", "qa-other"]


@pytest.mark.asyncio
async def test_the_floor_still_abstains_before_any_call() -> None:
    """Nothing above the floor means nothing to decide."""
    gate = _gate(ScriptedTransport(0.99, {"qa-right": 0.99}))
    low = _evidence("qa-low", "Irrelevant.", 0.10)

    selection, reason = await gate.select("Quan entrenen?", [low], [])

    assert selection.abstain is True
    assert reason == "floor"


@pytest.mark.asyncio
async def test_without_a_model_the_gate_is_the_floor() -> None:
    """A runtime with no decision model keeps today's behaviour unchanged."""
    gate = AnswerDecisionGate(policy=AnswerPolicy(floor=0.35))

    selection, reason = await gate.select("Quan entrenen?", [RIGHT, OTHER], [])

    assert reason == "floor"
    assert len(selection.evidence) == 2


@pytest.mark.asyncio
async def test_the_evaluation_transport_and_the_gate_agree_on_the_shape() -> None:
    """The runtime gate and the evaluated endpoint ask the same question.

    The endpoint answered the shipped evaluation; this gate is what production
    would run. If the two shapes drifted, the measurement would describe a
    system nobody runs.
    """
    transport = ScriptedTransport(0.85, {"qa-right": 0.91})
    gate = _gate(transport)

    await gate.select("Quan entrenen?", [RIGHT], [])

    asked = transport.question_names[0]
    assert asked == {
        "evidence.sufficient",
        "evidence.qa-right.relevant",
    }


def test_the_workers_ai_transport_is_the_one_the_gate_uses() -> None:
    """The production transport satisfies the port the gate depends on."""

    class _Runner:
        async def run(self, model: str, inputs: dict[str, object]) -> object:
            raise ModelUnavailableError("decision")

    transport = WorkersAISystemOneTransport(runner=_Runner())
    assert isinstance(transport, SystemOneTransport)


@pytest.mark.asyncio
async def test_decisions_tolerate_the_full_shortlist() -> None:
    """Seven items are one request, not seven."""
    items = tuple(_evidence(f"qa-{index}", f"Item {index}.", 0.5) for index in range(7))
    transport = ScriptedTransport(0.9, {item.source_id: 0.95 for item in items})
    gate = _gate(transport)

    selection, _ = await gate.select("Quan entrenen?", list(items), [])

    assert len(selection.evidence) == 7
    assert len(transport.calls) == 1
    assert len(transport.question_names[0]) == 8


def test_a_decision_carries_no_content_into_an_error() -> None:
    """A decision failure names the model, never the question or the evidence."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    logger = logging.getLogger("knowledge_bot.application.answer_decisions")
    logger.addHandler(handler)
    try:
        gate = _gate(BrokenTransport(ModelUnavailableError("decision")))
        asyncio.run(gate.select("Quan entrenen el meu fill?", [RIGHT], []))
    finally:
        logger.removeHandler(handler)

    assert records, "a degraded decision must be recorded"
    rendered = " ".join(record.getMessage() for record in records)
    assert "Quan entrenen" not in rendered
    assert "answer_decision_degraded" in rendered
    assert any(
        getattr(record, "cause", None) == "ModelUnavailableError" for record in records
    )


@pytest.mark.asyncio
async def test_a_decision_is_a_single_request_per_question_even_with_messages() -> None:
    """Message evidence and Q&A evidence are judged in the same request."""
    message_evidence = _evidence("msg:1", "Els dimarts.", 0.8)
    transport = ScriptedTransport(0.9, {"qa-right": 0.95, "msg:1": 0.96})
    gate = _gate(transport)

    selection, _ = await gate.select("Quan entrenen?", [RIGHT], [message_evidence])

    assert [item.source_id for item in selection.evidence] == ["qa-right", "msg:1"]
    assert len(transport.calls) == 1


def test_evidence_decision_defaults_are_harmless() -> None:
    """The decision record is a plain value with no required arguments."""
    decision = EvidenceDecision(sufficiency=0.5, relevance={})
    assert decision.duration_ms == 0.0
