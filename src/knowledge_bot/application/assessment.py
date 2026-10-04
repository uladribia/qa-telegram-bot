# SPDX-License-Identifier: MIT
"""Provider-neutral message assessment: intent and candidate relevance.

One inbound message needs two decisions: what the message *is* (its
communicative role) and which open questions it *answers*. The baseline wraps
the linear classifier and answers only the first; the System-One model answers
both in a single request, carrying the message once and asking one question per
candidate. Nothing here knows which server answered.
"""

import logging
from dataclasses import dataclass

from knowledge_bot.application.classifier import (
    LABELS,
    Classification,
    IntentScores,
    MessageClassifier,
    prefilter_classification,
    should_prefilter,
)
from knowledge_bot.domain.enums import IntentLabel
from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)
from knowledge_bot.models.assessment import MessageAssessment, RetroevalCandidate
from knowledge_bot.ports.assessment import MessageAssessmentModel
from knowledge_bot.ports.system_one import SystemOneTransport

logger = logging.getLogger(__name__)

#: Decision name of the single intent question every request carries.
INTENT_DECISION = "intent"

#: Prefix of one relevance decision per candidate question.
RELEVANCE_DECISION_PREFIX = "pair."

_INTENT_INSTRUCTIONS = (
    "Classify current_message by its communicative role in a parent-group conversation."
)
_INTENT_CRITERIA: dict[str, str] = {
    "question": (
        "The sender asks for information or requests something to be resolved."
    ),
    "knowledge_update": (
        "The sender states factual information that can be useful later."
    ),
    "correction": ("The sender corrects, replaces, or contradicts prior information."),
    "chitchat": ("Social conversation, acknowledgement, greeting, reaction, or noise."),
}

_RELEVANCE_INSTRUCTIONS = (
    "Does current_message directly and materially answer this candidate question? "
    "Topic overlap alone is not enough. A correction can be relevant when it "
    "directly answers or updates the question."
)
_RELEVANCE_CRITERIA: dict[str, str] = {
    "true": "It is safe to index current_message as evidence for this question.",
    "false": "It should not be indexed as evidence for this question.",
}


def build_message_decision_request(
    text: str, candidates: tuple[RetroevalCandidate, ...] = ()
) -> tuple[dict[str, object], dict[str, object]]:
    """Build the one canonical state and question set for a message.

    The runtime, the offline dataset builder, and the evaluator all send this
    exact request, so a model is trained and served on the same shape.

    Args:
        text: The current message, stated once as the decision state.
        candidates: The open questions the message could answer.

    Returns:
        The ``state`` object and the ``questions`` mapping.
    """
    state: dict[str, object] = {
        "current_message": text,
        "candidate_questions": [
            {
                "id": candidate.candidate_id,
                "question": candidate.question,
                "relation": candidate.relation,
            }
            for candidate in candidates
        ],
    }
    questions: dict[str, object] = {
        INTENT_DECISION: {
            "type": "choice",
            "instructions": _INTENT_INSTRUCTIONS,
            "criteria": dict(_INTENT_CRITERIA),
        }
    }
    for candidate in candidates:
        questions[relevance_decision_name(candidate.candidate_id)] = {
            "type": "noul",
            "instructions": _RELEVANCE_INSTRUCTIONS,
            "criteria": dict(_RELEVANCE_CRITERIA),
        }
    return state, questions


def relevance_decision_name(candidate_id: str) -> str:
    """Return the decision name carrying one candidate's relevance."""
    return f"{RELEVANCE_DECISION_PREFIX}{candidate_id}.relevant"


def parse_intent_decision(
    payload: dict[str, object],
) -> tuple[IntentScores, IntentLabel, float]:
    """Validate one ``choice`` intent answer and reduce it to a classification.

    Args:
        payload: The raw decision response.

    Returns:
        The intent scores, the winning label, and its top1-top2 margin.

    Raises:
        InvalidModelOutputError: The answer is absent, is not a choice, misses a
            label, carries an out-of-range or non-finite probability, or names
            a choice that is not the strongest label.
    """
    answer = _answer(payload, INTENT_DECISION)
    if answer.get("type") != "choice":
        raise InvalidModelOutputError("schema_validation")
    probabilities = _mapping(answer.get("probabilities"), "schema_validation")
    expected = {label.value for label in LABELS}
    if set(probabilities) != expected:
        raise InvalidModelOutputError("schema_validation")
    values = {label: _probability(probabilities[label]) for label in expected}
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in values:
        raise InvalidModelOutputError("schema_validation")
    ranked = sorted(values.items(), key=lambda item: item[1], reverse=True)
    if choice != ranked[0][0]:
        raise InvalidModelOutputError("schema_validation")
    scores = IntentScores(
        question=values[IntentLabel.QUESTION.value],
        knowledge_update=values[IntentLabel.KNOWLEDGE_UPDATE.value],
        correction=values[IntentLabel.CORRECTION.value],
        chitchat=values[IntentLabel.CHITCHAT.value],
    )
    return scores, IntentLabel(choice), ranked[0][1] - ranked[1][1]


def parse_relevance_decision(
    payload: dict[str, object], candidate_ids: tuple[str, ...]
) -> dict[str, float]:
    """Validate every candidate's ``noul`` answer and return the true probability.

    Args:
        payload: The raw decision response.
        candidate_ids: The candidates that must each have an answer.

    Returns:
        The probability that the message is relevant, per candidate id.

    Raises:
        InvalidModelOutputError: A candidate has no answer, or an answer is not
            a yes/no probability.
    """
    return parse_noul_decisions(
        payload,
        tuple(relevance_decision_name(candidate_id) for candidate_id in candidate_ids),
        keyed_by=candidate_ids,
    )


def parse_noul_decisions(
    payload: dict[str, object],
    names: tuple[str, ...],
    *,
    keyed_by: tuple[str, ...] | None = None,
) -> dict[str, float]:
    """Return the yes probability for each requested yes/no decision.

    The names are decision names exactly as they were asked. The listener shape
    happens to name them ``pair.<candidate>.relevant`` and the post-retrieval
    shape names them differently, so this function never rebuilds a name: it
    asks for what it is given. ``keyed_by`` re-labels the result when the
    caller wants a different key than the decision name.

    Args:
        payload: The raw decision response.
        names: The decision names, as they appear in the request.
        keyed_by: Optional labels to return the probabilities under.

    Returns:
        The probability per decision name, or per label when given.

    Raises:
        InvalidModelOutputError: A decision is missing or is not a usable
            yes/no answer.
    """
    labels = keyed_by if keyed_by is not None else names
    decided: dict[str, float] = {}
    for name, label in zip(names, labels, strict=True):
        answer = _answer(payload, name)
        if answer.get("type") != "noul":
            raise InvalidModelOutputError("schema_validation")
        raw = answer.get("noul")
        if raw is None:
            raw = _noul_true_probability(answer)
        decided[label] = _probability(raw)
    return decided


def _answer(payload: dict[str, object], decision: str) -> dict[str, object]:
    """Return one decision's answer object, or fail loudly."""
    answers = _mapping(payload.get("answers"), "schema_validation")
    answer = answers.get(decision)
    if not isinstance(answer, dict):
        raise InvalidModelOutputError("missing_content")
    return {str(key): value for key, value in answer.items()}


def _mapping(value: object, code: str) -> dict[str, object]:
    """Return a JSON object as a string-keyed mapping, or fail loudly."""
    if not isinstance(value, dict):
        raise InvalidModelOutputError(code)
    return {str(key): item for key, item in value.items()}


def _probability(value: object) -> float:
    """Return a finite probability in [0, 1], or fail loudly."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidModelOutputError("schema_validation")
    probability = float(value)
    if probability != probability or probability in (float("inf"), float("-inf")):
        raise InvalidModelOutputError("schema_validation")
    if not 0.0 <= probability <= 1.0:
        raise InvalidModelOutputError("schema_validation")
    return probability


def _noul_true_probability(answer: dict[str, object]) -> float:
    """Return the yes probability from a server that reports both options."""
    probabilities = _mapping(answer.get("probabilities"), "schema_validation")
    if "true" not in probabilities:
        raise InvalidModelOutputError("schema_validation")
    return _probability(probabilities["true"])


@dataclass(frozen=True, slots=True)
class BaselineAssessmentModel:
    """Assess with the linear classifier and no relevance opinion.

    An empty ``pair_relevance`` tells the pairing service that the
    deterministic policy still owns the decision.
    """

    classifier: MessageClassifier

    @property
    def confidence_threshold(self) -> float:
        """The confidence policy this backend applies."""
        return self.classifier.confidence_threshold

    @property
    def margin_threshold(self) -> float:
        """The margin policy this backend applies."""
        return self.classifier.margin_threshold

    async def assess(
        self,
        text: str,
        *,
        candidates: tuple[RetroevalCandidate, ...] = (),
    ) -> MessageAssessment:
        """Classify the message with the baseline classifier."""
        del candidates
        return MessageAssessment(
            classification=await self.classifier.classify(text),
            pair_relevance={},
        )

    def is_question(self, classification: Classification) -> bool:
        """Whether the message is a confident question."""
        return self.classifier.is_question(classification)

    def is_answer_like(self, classification: Classification) -> bool:
        """Whether the message is a confident factual update or correction."""
        return self.classifier.is_answer_like(classification)


@dataclass(frozen=True, slots=True)
class SystemOneAssessmentModel:
    """Assess one message with a decision service, degrading when it cannot.

    Two switches keep the risk bounded. ``include_relevance`` decides whether
    the service is asked which open questions the message answers at all; with
    it off, the model classifies and the deterministic pairing policy still
    chooses the pair. ``fallback`` is the baseline decision model, used when the
    service is unreachable or answers something unusable, so an outage
    degrades the listener instead of losing the message.
    """

    transport: SystemOneTransport
    model: str
    confidence_threshold: float = 0.60
    margin_threshold: float = 0.15
    relevance_threshold: float = 0.80
    relevance_margin: float = 0.15
    include_relevance: bool = False
    fallback: MessageAssessmentModel | None = None

    async def assess(
        self,
        text: str,
        *,
        candidates: tuple[RetroevalCandidate, ...] = (),
    ) -> MessageAssessment:
        """Decide the message's intent, and optionally its candidate relevance.

        Args:
            text: The current message text.
            candidates: The open questions the message could answer.

        Returns:
            The classification and, when relevance was asked for and answered,
            the per-candidate relevance probabilities.

        Raises:
            InvalidModelOutputError: The service answered something other than
                the decisions that were asked for, and no fallback is configured.
            ModelUnavailableError: The service could not be reached, and no
                fallback is configured.
        """
        if should_prefilter(text):
            return MessageAssessment(
                classification=prefilter_classification(), pair_relevance={}
            )
        asked = candidates if self.include_relevance else ()
        candidate_ids = tuple(candidate.candidate_id for candidate in asked)
        state, questions = build_message_decision_request(text, asked)
        try:
            payload = await self.transport.decide(
                model=self.model, state=state, questions=questions
            )
            scores, best_label, margin = parse_intent_decision(payload)
            relevance = parse_relevance_decision(payload, candidate_ids)
        except (InvalidModelOutputError, ModelUnavailableError) as error:
            if self.fallback is None:
                raise
            return await self._degrade(text, error)
        return MessageAssessment(
            classification=Classification(
                scores=scores,
                best_label=best_label,
                best_score=scores.best()[1],
                margin=margin,
                embedding=(),
            ),
            pair_relevance=relevance,
        )

    async def _degrade(
        self, text: str, cause: InvalidModelOutputError | ModelUnavailableError
    ) -> MessageAssessment:
        """Answer from the baseline decision, recording why it was needed."""
        fallback = self.fallback
        if fallback is None:
            raise ModelUnavailableError("decision")
        logger.warning(
            "decision_fallback_to_baseline",
            extra={
                "decision_model": self.model,
                "cause": type(cause).__name__,
                "asked_relevance": self.include_relevance,
            },
        )
        return await fallback.assess(text)

    def is_question(self, classification: Classification) -> bool:
        """Whether the message is a confident question."""
        return (
            classification.is_confident_with(
                self.confidence_threshold, self.margin_threshold
            )
            and classification.best_label is IntentLabel.QUESTION
        )

    def is_answer_like(self, classification: Classification) -> bool:
        """Whether the message is a confident factual update or correction."""
        return classification.is_confident_with(
            self.confidence_threshold, self.margin_threshold
        ) and classification.best_label in (
            IntentLabel.KNOWLEDGE_UPDATE,
            IntentLabel.CORRECTION,
        )


#: Decision name of the "is this evidence enough at all" question.
SUFFICIENCY_DECISION = "evidence.sufficient"

#: Decision name of the proactive trigger: is this message an open question.
PROACTIVE_DECISION = "open_question"

_SUFFICIENCY_INSTRUCTIONS = (
    "Can the question in the state be answered from the evidence provided, "
    "without inventing anything? Judge only from the evidence."
)
_EVIDENCE_INSTRUCTIONS = (
    "Does this evidence item answer the question asked in the state? "
    "Being on the same topic is not enough; it must carry the fact the "
    "question asks for."
)
_OPEN_QUESTION_INSTRUCTIONS = (
    "Is this message an open question that deserves an answer from the "
    "group's knowledge, rather than social conversation, a statement, or a "
    "correction?"
)


def build_answer_decision_request(
    question: str,
    evidence: tuple[tuple[str, str], ...],
) -> tuple[dict[str, object], dict[str, object]]:
    """Build the post-retrieval decision request for one question.

    One state carries the question and the shortlist the cosine retrieval
    already chose; the questions are the two decisions the answer path needs:
    whether anything here is enough, and which items actually answer it. The
    shortlist stays small by construction, because the cosine floor is what
    bounds the tokens this request may cost.

    Args:
        question: The question that must be answered.
        evidence: The shortlisted ``(evidence_id, text)`` pairs, in rank order.

    Returns:
        The ``state`` object and the ``questions`` mapping.
    """
    state: dict[str, object] = {
        "question": question,
        "evidence": [
            {"id": evidence_id, "text": text} for evidence_id, text in evidence
        ],
    }
    questions: dict[str, object] = {
        SUFFICIENCY_DECISION: {
            "type": "noul",
            "instructions": _SUFFICIENCY_INSTRUCTIONS,
            "criteria": {
                "true": "The evidence contains what the question asks for.",
                "false": "The evidence does not answer the question.",
            },
        }
    }
    for evidence_id, _ in evidence:
        questions[f"evidence.{evidence_id}.relevant"] = {
            "type": "noul",
            "instructions": _EVIDENCE_INSTRUCTIONS,
            "criteria": {
                "true": "This item answers the question asked.",
                "false": "This item does not answer the question asked.",
            },
        }
    return state, questions


def build_proactive_question() -> dict[str, object]:
    """Return the proactive-trigger question added to a listener request.

    The trigger rides in the request the listener already makes for this
    message, so a proactive group pays no extra call for it.
    """
    return {
        "type": "noul",
        "instructions": _OPEN_QUESTION_INSTRUCTIONS,
        "criteria": {
            "true": "The message is an open question worth answering.",
            "false": "It is not an open question worth answering.",
        },
    }


def parse_sufficiency_decision(payload: dict[str, object]) -> float:
    """Return the probability that the evidence is enough to answer.

    Args:
        payload: The raw decision response.

    Returns:
        The yes probability for ``evidence.sufficient``.

    Raises:
        InvalidModelOutputError: The sufficiency answer is missing or unusable.
    """
    return parse_relevance_decision(payload, (SUFFICIENCY_DECISION,))[
        SUFFICIENCY_DECISION
    ]


def parse_evidence_relevance(
    payload: dict[str, object], evidence_ids: tuple[str, ...]
) -> dict[str, float]:
    """Return the per-item relevance probabilities of a shortlist.

    Args:
        payload: The raw decision response.
        evidence_ids: The evidence ids that must each have an answer.

    Returns:
        The probability that each item answers the question.

    Raises:
        InvalidModelOutputError: An item has no usable answer.
    """
    names = tuple(f"evidence.{evidence_id}.relevant" for evidence_id in evidence_ids)
    scored = parse_noul_decisions(payload, names)
    return {
        evidence_id: scored[f"evidence.{evidence_id}.relevant"]
        for evidence_id in evidence_ids
    }
