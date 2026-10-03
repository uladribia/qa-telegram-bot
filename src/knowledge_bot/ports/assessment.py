# SPDX-License-Identifier: MIT
"""Port for the model that assesses one inbound message."""

from typing import Protocol, runtime_checkable

from knowledge_bot.application.classifier import Classification
from knowledge_bot.models.assessment import MessageAssessment, RetroevalCandidate


@runtime_checkable
class MessageAssessmentModel(Protocol):
    """Decides what one message is and which open questions it answers.

    An implementation performs at most one model call per message: the intent
    decision and every candidate relevance decision travel together.
    """

    @property
    def confidence_threshold(self) -> float:
        """The confidence policy this backend applies."""
        ...

    @property
    def margin_threshold(self) -> float:
        """The margin policy this backend applies."""
        ...

    async def assess(
        self,
        text: str,
        *,
        candidates: tuple[RetroevalCandidate, ...] = (),
    ) -> MessageAssessment:
        """Assess one message against the open question candidates.

        Args:
            text: The message text to assess.
            candidates: The open questions this message could answer.

        Returns:
            The message's classification and per-candidate relevance.
        """
        ...

    def is_question(self, classification: Classification) -> bool:
        """Whether the message is a confident question."""
        ...

    def is_answer_like(self, classification: Classification) -> bool:
        """Whether the message is a confident factual update or correction."""
        ...
