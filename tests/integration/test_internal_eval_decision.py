# SPDX-License-Identifier: MIT
"""The decision-evaluation endpoint: reachable, side-effect free, honest."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from knowledge_bot.api.app import create_app
from knowledge_bot.api.context import internal_context
from knowledge_bot.application.assessment import (
    BaselineAssessmentModel,
    SystemOneAssessmentModel,
)
from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.cloudflare.system_one import (
    EVAL_DECISION_MODEL,
    WorkersAISystemOneTransport,
)
from knowledge_bot.infrastructure.context import AppContext
from knowledge_bot.models.assessment import RetroevalCandidate
from tests.fakes.ai import FakeEmbedder
from tests.fakes.context import build_test_context

pytestmark = pytest.mark.integration

CASE = {
    "case_id": "c1",
    "text": "Els dimarts a les sis.",
    "candidates": [
        {
            "candidate_id": "k1",
            "question_message_id": "q1",
            "question": "Quan entrenen?",
            "relation": "temporal_window",
        },
        {
            "candidate_id": "k2",
            "question_message_id": "q2",
            "question": "Quant costa la inscripció?",
            "relation": "temporal_window",
        },
    ],
}


class _Items:
    """The in-memory repositories' item mapping, for counting in one test."""

    _items: dict[str, object] = {}


class _VectorRecords:
    """The in-memory vector store's record mapping, for counting in one test."""

    records: dict[str, object] = {}


class _AnswersTheRequest:
    """Marks a payload that must be built from the request it answers."""

    def __init__(self, build: Callable[[dict[str, object]], object]) -> None:
        """Store the builder."""
        self._build = build

    def build(self, inputs: dict[str, object]) -> object:
        """Answer one recorded request."""
        return self._build(inputs)


class RecordingRunner:
    """A Workers AI binding fake that records calls and replays a payload."""

    def __init__(self, payload: object | Exception) -> None:
        """Configure the payload or failure every call answers with."""
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(self, model: str, inputs: dict[str, object]) -> object:
        """Record the call and answer with the configured payload."""
        self.calls.append((model, inputs))
        if isinstance(self.payload, Exception):
            raise self.payload
        builder = self.payload
        if isinstance(builder, _AnswersTheRequest):
            return builder.build(inputs)
        return builder


def _answer(name: str) -> dict[str, object]:
    """Return one well-formed choice answer."""
    probabilities = {
        "question": 0.02,
        "knowledge_update": 0.95,
        "correction": 0.02,
        "chitchat": 0.02,
    }
    del name
    return {
        "type": "choice",
        "choice": "knowledge_update",
        "probabilities": probabilities,
    }


def _noul(probability: float) -> dict[str, object]:
    """Return one well-formed relevance answer."""
    return {"type": "noul", "noul": probability}


def _payload_for(questions: dict[str, object]) -> dict[str, object]:
    """Answer every question in a request, relevance high for the first."""
    answers: dict[str, object] = {"intent": _answer("intent")}
    first = True
    for name in questions:
        if name == "intent":
            continue
        answers[name] = _noul(0.95 if first else 0.20)
        first = False
    return {"answers": answers}


def _answer_the_request() -> _AnswersTheRequest:
    """Answer whatever a request asked, relevance high for the first candidate."""
    return _AnswersTheRequest(_payload_for_request)


def _payload_for_request(inputs: dict[str, object]) -> object:
    """Answer one request's questions from the questions it actually sent."""
    questions = cast(dict[str, object], inputs["questions"])
    return _payload_for(questions)


def _client(context: AppContext) -> TestClient:
    """Return a test client for the internal routes."""
    return TestClient(create_app(lambda request: context))


def _with_evaluator(context: AppContext, runner: RecordingRunner) -> AppContext:
    """Return a context whose evaluation backend is the given binding fake."""
    model = SystemOneAssessmentModel(
        transport=WorkersAISystemOneTransport(runner=runner),
        model=EVAL_DECISION_MODEL,
        include_relevance=True,
        fallback=None,
    )
    return replace(context, decision_evaluator=lambda: model)


def _post(
    context: AppContext, body: dict[str, object], key: str = "internal"
) -> Response:
    """Post an evaluation request with the internal key."""
    return _client(context).post(
        "/internal/eval/decision",
        json=body,
        headers={"X-Internal-Key": key},
    )


def test_the_endpoint_refuses_a_bad_key() -> None:
    """The evaluation surface is not reachable without the internal key."""
    context, _ = build_test_context()

    response = _post(context, {"backend": "clef-flash", "cases": [CASE]}, key="wrong")

    assert response.status_code == 401


def test_the_endpoint_refuses_an_empty_request() -> None:
    """A body with no cases is a bad request, not an empty evaluation."""
    context, _ = build_test_context()

    response = _post(context, {"backend": "clef-flash", "cases": []})

    assert response.status_code == 422


def test_one_case_is_one_request_carrying_every_decision() -> None:
    """Intent plus every candidate relevance travel in a single call."""
    runner = RecordingRunner(_answer_the_request())
    context, _ = build_test_context()
    context = _with_evaluator(context, runner)

    response = _post(context, {"backend": "clef-flash", "cases": [CASE]})

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["best_label"] == "knowledge_update"
    assert result["selected_candidate_id"] == "k1"
    assert set(result["relevance"]) == {"k1", "k2"}
    calls: list[tuple[str, dict[str, object]]] = runner.calls
    assert len(calls) == 1
    sent = cast(dict[str, object], calls[0][1]["questions"])
    assert len(sent) == 3, "intent plus one relevance per candidate"


def test_five_candidates_stay_one_request_with_six_questions() -> None:
    """The one-pass invariant holds at the request limit."""
    runner = RecordingRunner(_answer_the_request())
    context, _ = build_test_context()
    context = _with_evaluator(context, runner)
    case = {
        "case_id": "wide",
        "text": "Els dimarts a les sis.",
        "candidates": [
            {
                "candidate_id": f"k{index}",
                "question_message_id": f"q{index}",
                "question": f"Pregunta {index}?",
                "relation": "temporal_window",
            }
            for index in range(5)
        ],
    }

    response = _post(context, {"backend": "clef-flash", "cases": [case]})

    assert response.status_code == 200, response.text
    assert len(runner.calls) == 1
    questions = cast(dict[str, object], runner.calls[0][1]["questions"])
    assert len(questions) == 6
    assert response.json()["results"][0]["selected_candidate_id"] == "k0"


def test_a_request_above_the_candidate_limit_is_refused() -> None:
    """Six candidates are a 422, not a silently truncated evaluation."""
    context, _ = build_test_context()
    case = {
        "case_id": "too-wide",
        "text": "Els dimarts a les sis.",
        "candidates": [
            {
                "candidate_id": f"k{index}",
                "question_message_id": f"q{index}",
                "question": f"Pregunta {index}?",
                "relation": "temporal_window",
            }
            for index in range(6)
        ],
    }

    response = _post(context, {"backend": "clef-flash", "cases": [case]})

    assert response.status_code == 422


def test_a_provider_failure_is_an_error_and_never_a_baseline_answer() -> None:
    """A failed decision is reported, not quietly replaced."""
    runner = RecordingRunner(ModelUnavailableError("decision"))
    context, _ = build_test_context()
    context = _with_evaluator(context, runner)

    response = _post(context, {"backend": "clef-flash", "cases": [CASE]})

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["error"] == "ModelUnavailableError"
    assert "best_label" not in result
    assert "selected_candidate_id" not in result


def test_malformed_output_is_an_error_too() -> None:
    """An answer that is not the decision asked for is a failure, not a guess."""
    runner = RecordingRunner({"answers": {"intent": {"type": "choice"}}})
    context, _ = build_test_context()
    context = _with_evaluator(context, runner)

    response = _post(context, {"backend": "clef-flash", "cases": [CASE]})

    result = response.json()["results"][0]
    assert result["error"] == "InvalidModelOutputError"


def test_the_baseline_backend_needs_no_decision_model() -> None:
    """The baseline arm of the evaluation runs on the deployed classifier."""
    context, _ = build_test_context(
        embedder=FakeEmbedder(by_text={"Els dimarts a les sis.": [0.0, 1.0]})
    )

    response = _post(context, {"backend": "baseline", "cases": [CASE]})

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["backend"] == "baseline"
    assert result["relevance"] == {}
    assert result["selected_candidate_id"] is None, (
        "with no relevance opinion the baseline pairs only when one candidate is open"
    )


def test_the_endpoint_persists_nothing() -> None:
    """No message, pair, projection, or delivery is written by an evaluation."""
    runner = RecordingRunner(_answer_the_request())
    context, _ = build_test_context()
    context = _with_evaluator(context, runner)
    messages = cast(_Items, context.ingestor.messages)
    pairs = cast(_Items, context.pairing.candidates)
    before = (len(messages._items), len(pairs._items))

    response = _post(context, {"backend": "clef-flash", "cases": [CASE]})

    assert response.status_code == 200
    assert (len(messages._items), len(pairs._items)) == before
    vectors = cast(_VectorRecords, context.answer.retrieval.vectors).records
    assert vectors == {}


def test_the_production_listener_is_still_the_baseline() -> None:
    """Wiring an evaluation backend must not move user traffic onto it."""
    context, _ = build_test_context()

    assert isinstance(context.listener.assessment, BaselineAssessmentModel)
    assert isinstance(context.pairing.assessment, BaselineAssessmentModel)
    assert context.settings.decision_backend.value == "baseline"


@pytest.mark.asyncio
async def test_the_transport_turns_a_binding_failure_into_a_model_error() -> None:
    """A binding that raises becomes one decision error, not an exception."""
    transport = WorkersAISystemOneTransport(
        runner=RecordingRunner(RuntimeError("binding exploded"))
    )

    with pytest.raises(ModelUnavailableError):
        await transport.decide(model=EVAL_DECISION_MODEL, state="s", questions={})


@pytest.mark.asyncio
async def test_the_transport_decodes_a_json_string_response() -> None:
    """A binding that answers with JSON text is decoded before parsing."""
    import json

    payload = json.dumps({"answers": {"intent": _answer("intent")}})
    transport = WorkersAISystemOneTransport(runner=RecordingRunner(payload))

    decoded = await transport.decide(model=EVAL_DECISION_MODEL, state="s", questions={})

    answers = cast(dict[str, object], decoded["answers"])
    intent = cast(dict[str, object], answers["intent"])
    assert intent["choice"] == "knowledge_update"


def test_candidates_are_read_as_domain_values() -> None:
    """The endpoint's candidate DTOs map onto the domain candidate."""
    candidate = RetroevalCandidate(
        candidate_id="k1",
        question_message_id="q1",
        question="Quan entrenen?",
        relation="explicit_reply",
    )

    assert candidate.relation == "explicit_reply"
    assert asyncio.iscoroutinefunction(internal_context)
