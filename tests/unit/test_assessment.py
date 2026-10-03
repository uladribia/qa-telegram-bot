# SPDX-License-Identifier: MIT
"""Unit coverage for the System-One request shape and its strict parser."""

import math
from typing import cast

import pytest

from knowledge_bot.application.assessment import (
    BaselineAssessmentModel,
    SystemOneAssessmentModel,
    build_message_decision_request,
    parse_intent_decision,
    parse_relevance_decision,
    relevance_decision_name,
)
from knowledge_bot.domain.enums import IntentLabel
from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)
from knowledge_bot.models.assessment import RetroevalCandidate
from knowledge_bot.ports.system_one import SystemOneTransport


class RecordingTransport:
    """A decision service that records every request and replays a payload."""

    def __init__(self, payload: dict[str, object]) -> None:
        """Configure the payload every call answers with."""
        self.payload = payload
        self.models: list[str] = []
        self.question_counts: list[int] = []

    async def decide(
        self,
        *,
        model: str,
        state: object,
        questions: dict[str, object],
    ) -> dict[str, object]:
        """Record the request and answer with the configured payload."""
        del state
        self.models.append(model)
        self.question_counts.append(len(questions))
        return self.payload


def _candidate(candidate_id: str) -> RetroevalCandidate:
    """Return one candidate question."""
    return RetroevalCandidate(
        candidate_id=candidate_id,
        question_message_id=f"stored-{candidate_id}",
        question=f"Què preguntem {candidate_id}?",
        relation="temporal_window",
    )


def _intent_answer(choice: str = "knowledge_update") -> dict[str, object]:
    """Return a well-formed intent answer."""
    probabilities = {label.value: 0.02 for label in IntentLabel}
    probabilities[choice] = 0.94
    return {"type": "choice", "choice": choice, "probabilities": probabilities}


def _noul_answer(probability: float) -> dict[str, object]:
    """Return a well-formed relevance answer."""
    return {"type": "noul", "noul": probability}


def _model(
    transport: SystemOneTransport,
    *,
    confidence_threshold: float = 0.60,
    margin_threshold: float = 0.15,
    include_relevance: bool = False,
    fallback: BaselineAssessmentModel | None = None,
) -> SystemOneAssessmentModel:
    """Build the model under test."""
    return SystemOneAssessmentModel(
        transport=transport,
        model="tev1:0.8b",
        confidence_threshold=confidence_threshold,
        margin_threshold=margin_threshold,
        include_relevance=include_relevance,
        fallback=fallback,
    )


def _question(spec: object) -> dict[str, object]:
    """Return one typed question as a plain mapping."""
    assert isinstance(spec, dict)
    return cast(dict[str, object], spec)


def test_state_states_the_message_once_and_candidates_separately() -> None:
    """One state carries the message; candidates only reference it."""
    state, questions = build_message_decision_request(
        "A les sis.", (_candidate("c1"), _candidate("c2"))
    )
    expected_state: dict[str, object] = {
        "current_message": "A les sis.",
        "candidate_questions": [
            {
                "id": "c1",
                "question": "Què preguntem c1?",
                "relation": "temporal_window",
            },
            {
                "id": "c2",
                "question": "Què preguntem c2?",
                "relation": "temporal_window",
            },
        ],
    }
    assert state == expected_state
    expected_names = {
        "intent",
        relevance_decision_name("c1"),
        relevance_decision_name("c2"),
    }
    assert set(questions) == expected_names
    intent = _question(questions["intent"])
    assert intent["type"] == "choice"
    criteria = intent["criteria"]
    assert isinstance(criteria, dict)
    assert set(criteria) == {label.value for label in IntentLabel}
    relevance = _question(questions[relevance_decision_name("c1")])
    assert relevance["type"] == "noul"
    relevance_criteria = relevance["criteria"]
    assert isinstance(relevance_criteria, dict)
    assert set(relevance_criteria) == {"true", "false"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "expected"),
    ((0, 1), (1, 2), (5, 6)),
)
async def test_one_request_per_message(count: int, expected: int) -> None:
    """Candidates multiply decisions inside one request, never requests."""
    candidates = tuple(_candidate(f"c{index}") for index in range(count))
    answers: dict[str, object] = {"intent": _intent_answer()}
    for candidate in candidates:
        answers[relevance_decision_name(candidate.candidate_id)] = _noul_answer(0.9)
    transport = RecordingTransport({"answers": answers})
    model = _model(transport, include_relevance=True)

    assessment = await model.assess("A les sis.", candidates=candidates)

    assert len(transport.question_counts) == 1
    assert transport.question_counts[0] == expected
    assert assessment.classification.best_label is IntentLabel.KNOWLEDGE_UPDATE
    assert set(assessment.pair_relevance) == {
        candidate.candidate_id for candidate in candidates
    }


@pytest.mark.asyncio
async def test_prefilter_makes_no_request() -> None:
    """An acknowledgement is decided without spending a request."""
    transport = RecordingTransport({"answers": {}})

    assessment = await _model(transport).assess("gràcies")

    assert transport.models == []
    assert transport.question_counts == []
    assert assessment.classification.prefiltered
    assert assessment.classification.best_label is IntentLabel.CHITCHAT
    assert assessment.pair_relevance == {}


def test_intent_decision_reports_the_margin() -> None:
    """The margin is the gap between the top two labels."""
    scores, label, margin = parse_intent_decision(
        {"answers": {"intent": _intent_answer()}}
    )
    assert label is IntentLabel.KNOWLEDGE_UPDATE
    assert margin == pytest.approx(0.92)
    assert scores.knowledge_update == pytest.approx(0.94)


@pytest.mark.parametrize(
    ("payload",),
    (
        ({"answers": {}},),
        ({"answers": {"intent": {"type": "score", "score": 1.0}}},),
        (
            {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "question",
                        "probabilities": {"question": 1.0},
                    }
                }
            },
        ),
        (
            {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "not_a_label",
                        "probabilities": {
                            "question": 0.2,
                            "knowledge_update": 0.2,
                            "correction": 0.2,
                            "chitchat": 0.2,
                        },
                    }
                }
            },
        ),
        (
            {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "chitchat",
                        "probabilities": {
                            "question": 0.4,
                            "knowledge_update": 0.4,
                            "correction": 0.1,
                            "chitchat": 0.1,
                        },
                    }
                }
            },
        ),
        (
            {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "knowledge_update",
                        "probabilities": {
                            "question": math.nan,
                            "knowledge_update": 1.0,
                            "correction": 0.0,
                            "chitchat": 0.0,
                        },
                    }
                }
            },
        ),
        (
            {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "knowledge_update",
                        "probabilities": {
                            "question": -0.2,
                            "knowledge_update": 1.0,
                            "correction": 0.0,
                            "chitchat": 0.2,
                        },
                    }
                }
            },
        ),
        (
            {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "knowledge_update",
                        "probabilities": {
                            "question": 0.5,
                            "knowledge_update": 1.4,
                            "correction": 0.0,
                            "chitchat": 0.0,
                        },
                    }
                }
            },
        ),
    ),
)
def test_malformed_intent_output_is_rejected(payload: dict[str, object]) -> None:
    """A broken decision fails loudly instead of guessing a label."""
    with pytest.raises(InvalidModelOutputError):
        parse_intent_decision(payload)


@pytest.mark.parametrize(
    "answer",
    (
        {},
        {"type": "choice", "choice": "true", "probabilities": {"true": 0.9}},
        {"type": "noul", "noul": math.nan},
        {"type": "noul", "noul": -0.1},
        {"type": "noul", "noul": 1.4},
    ),
)
def test_malformed_relevance_output_is_rejected(answer: dict[str, object]) -> None:
    """A broken relevance answer is an error, never a silent zero."""
    payload: dict[str, object] = {"answers": {relevance_decision_name("c1"): answer}}
    with pytest.raises(InvalidModelOutputError):
        parse_relevance_decision(payload, ("c1",))


def test_a_missing_candidate_answer_is_an_error() -> None:
    """Every candidate asked must be answered."""
    payload: dict[str, object] = {
        "answers": {relevance_decision_name("c1"): _noul_answer(0.9)}
    }
    with pytest.raises(InvalidModelOutputError):
        parse_relevance_decision(payload, ("c1", "c2"))


def test_a_server_may_report_relevance_without_the_short_key() -> None:
    """A yes/no answer carrying only its probabilities still parses."""
    payload: dict[str, object] = {
        "answers": {
            relevance_decision_name("c1"): {
                "type": "noul",
                "probabilities": {"true": 0.93, "false": 0.07},
            }
        }
    }
    assert parse_relevance_decision(payload, ("c1",)) == {"c1": pytest.approx(0.93)}


@pytest.mark.asyncio
async def test_confidence_gates_follow_the_configured_policy() -> None:
    """Intent answers are only questions or answers above the thresholds."""
    transport = RecordingTransport({"answers": {"intent": _intent_answer("question")}})
    model = _model(transport)

    assessment = await model.assess("Quan entrenem?")

    assert model.is_question(assessment.classification)
    assert not model.is_answer_like(assessment.classification)


@pytest.mark.asyncio
async def test_ambiguous_intent_is_neither_question_nor_answer() -> None:
    """A weak top label is a refusal, not a guess."""
    weak = {
        "type": "choice",
        "choice": "knowledge_update",
        "probabilities": {
            "question": 0.30,
            "knowledge_update": 0.35,
            "correction": 0.20,
            "chitchat": 0.15,
        },
    }
    transport = RecordingTransport({"answers": {"intent": weak}})
    model = _model(transport)

    assessment = await model.assess("A les sis.")

    assert not model.is_question(assessment.classification)
    assert not model.is_answer_like(assessment.classification)


@pytest.mark.asyncio
async def test_a_choice_that_is_not_the_strongest_label_is_rejected() -> None:
    """An answer disagreeing with its own probabilities is malformed."""
    inconsistent = {
        "type": "choice",
        "choice": "chitchat",
        "probabilities": {
            "question": 0.20,
            "knowledge_update": 0.40,
            "correction": 0.30,
            "chitchat": 0.10,
        },
    }
    transport = RecordingTransport({"answers": {"intent": inconsistent}})
    model = _model(transport)

    with pytest.raises(InvalidModelOutputError):
        await model.assess("A les sis.")


@pytest.mark.asyncio
async def test_relevance_off_asks_one_question_and_defers_pairing() -> None:
    """With the floor on, candidates stay out of the request and out of the answer."""
    transport = RecordingTransport({"answers": {"intent": _intent_answer()}})
    model = _model(transport)

    assessment = await model.assess(
        "A les sis.", candidates=(_candidate("c1"), _candidate("c2"))
    )

    assert transport.question_counts == [1]
    assert assessment.pair_relevance == {}
    assert assessment.classification.best_label is IntentLabel.KNOWLEDGE_UPDATE


class _BrokenTransport:
    """A decision service that is not answering."""

    def __init__(self, error: Exception) -> None:
        """Configure the failure the service reports."""
        self.error = error

    async def decide(
        self,
        *,
        model: str,
        state: object,
        questions: dict[str, object],
    ) -> dict[str, object]:
        """Fail the way an unreachable service fails."""
        del model, state, questions
        raise self.error


def _baseline_fallback() -> BaselineAssessmentModel:
    """Return a baseline decision model that recognises one text."""
    from knowledge_bot.application.classifier import (
        ClassifierHead,
        MessageClassifier,
    )

    class _TextEmbedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    head = ClassifierHead(
        labels=(
            IntentLabel.QUESTION,
            IntentLabel.KNOWLEDGE_UPDATE,
            IntentLabel.CORRECTION,
            IntentLabel.CHITCHAT,
        ),
        coef=((2.0, 0.0), (0.0, 2.0), (0.0, 0.0), (0.0, 0.0)),
        intercept=(0.0, 0.0, 0.0, 0.0),
    )
    return BaselineAssessmentModel(
        MessageClassifier(embedder=_TextEmbedder(), head=head)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        ModelUnavailableError("decision"),
        InvalidModelOutputError("schema_validation"),
    ),
)
async def test_an_unusable_service_degrades_to_the_baseline(error: Exception) -> None:
    """A decision-service outage must not lose the message."""
    model = _model(_BrokenTransport(error), fallback=_baseline_fallback())

    assessment = await model.assess("Quan entrenem?")

    assert assessment.classification.best_label is IntentLabel.QUESTION
    assert assessment.classification.embedding
    assert assessment.pair_relevance == {}


@pytest.mark.asyncio
async def test_without_a_fallback_the_service_failure_surfaces() -> None:
    """With no baseline configured, the failure is the caller's to handle."""
    model = _model(_BrokenTransport(ModelUnavailableError("decision")))

    with pytest.raises(ModelUnavailableError):
        await model.assess("Quan entrenem?")
