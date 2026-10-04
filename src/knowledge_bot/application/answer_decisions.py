# SPDX-License-Identifier: MIT
"""Decide, after retrieval, whether the evidence is enough and what answers.

Two decisions sit between retrieval and the generator. The cosine floor has
already chosen a shortlist and bounded its size, so nothing here re-ranks the
corpus: this gate asks a decision model two questions about that shortlist —
is anything in it enough to answer, and which items actually answer — and hands
the generator only what survives.

Both decisions come from one request. The state travels once, and each extra
question costs far less than another round trip, so splitting them would buy
isolation this module already provides by failing closed.

Failure is the important case. A decision service that is unreachable, slow, or
unusable must not become a reason to answer from evidence nobody judged, nor a
reason to lose the question: the gate degrades to the previous behaviour — the
floor alone — and records why. The generator's own abstention stays as the
backstop underneath, because one remote call should never be the only thing
between a question and a wrong answer.
"""

import logging
import time
from dataclasses import dataclass, replace

from knowledge_bot.application.answer_policy import AnswerPolicy, EvidenceSelection
from knowledge_bot.application.assessment import (
    AnswerDecisionModel,
    EvidenceDecision,
)
from knowledge_bot.application.retrieval import Evidence
from knowledge_bot.domain.errors import (
    InvalidModelOutputError,
    ModelUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AnswerDecisionSettings:
    """Where the two decision thresholds sit for this runtime."""

    sufficiency_threshold: float = 0.50
    selection_threshold: float = 0.90


@dataclass(frozen=True, slots=True)
class AnswerDecisionGate:
    """Turn a floor selection into a decision, or into the previous behaviour."""

    policy: AnswerPolicy
    model: AnswerDecisionModel | None = None
    settings: AnswerDecisionSettings = AnswerDecisionSettings()

    async def select(
        self,
        question: str,
        qa: list[Evidence],
        messages: list[Evidence],
    ) -> tuple[EvidenceSelection, str]:
        """Decide what the generator may see.

        Args:
            question: The question that must be answered.
            qa: The Q&A candidates retrieval returned.
            messages: The message-evidence candidates retrieval returned.

        Returns:
            The selection, and the reason it was reached: ``floor`` when the
            decision model was absent or unusable, otherwise ``decision``.
        """
        floor = self.policy.select(qa, messages)
        if floor.abstain or self.model is None:
            return floor, "floor"
        started = time.perf_counter()
        try:
            decided = await self.model.decide_evidence(
                question,
                tuple((item.source_id, item.text) for item in floor.evidence),
            )
        except (ModelUnavailableError, InvalidModelOutputError) as error:
            logger.warning(
                "answer_decision_degraded",
                extra={
                    "cause": type(error).__name__,
                    "candidates": len(floor.evidence),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return floor, "floor"
        return self._apply(floor, decided), "decision"

    def _apply(
        self, floor: EvidenceSelection, decided: EvidenceDecision
    ) -> EvidenceSelection:
        """Apply one decision to the floor selection."""
        if decided.sufficiency < self.settings.sufficiency_threshold:
            return EvidenceSelection(abstain=True)
        kept = tuple(
            item
            for item in floor.evidence
            if decided.relevance.get(item.source_id, 0.0)
            >= self.settings.selection_threshold
        )
        if not kept:
            # The model says the shortlist is enough and then keeps nothing, or
            # it kept nothing at all. Either way there is nothing to answer
            # from, and the honest outcome is the abstention.
            return EvidenceSelection(abstain=True)
        return replace(floor, evidence=kept, abstain=False)
