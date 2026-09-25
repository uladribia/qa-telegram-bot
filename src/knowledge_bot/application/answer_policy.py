# SPDX-License-Identifier: MIT
"""Answer evidence selection: one floor, then let the model answer.

Every question that has usable evidence is answered by the generator from that
evidence, and only a question with no usable candidate abstains. The evidence
set is the Q&A items that clear a single similarity floor, best first.

Why no verbatim echo: an echo has no ability to decline. Measured on the
abstention set, the previous fixed-threshold rule answered 98.7% of unknown
questions with the nearest document's answer. Sending the question to the model
instead lets it return ``insufficient``, which it does for almost every unknown
question. The cost is one grounded generation per answered question, measured
and recorded in ``docs/experiments.md``.

The floor is the only knob. It is ordinary configuration, tuned locally and
adjustable in production without a code change.
"""

from dataclasses import dataclass, field

from knowledge_bot.application.retrieval import Evidence

MAX_QA_EVIDENCE = 5
MAX_MESSAGE_EVIDENCE = 3


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    """The evidence the model may use, or nothing to answer from."""

    abstain: bool
    evidence: tuple[Evidence, ...] = ()

    @property
    def mode(self) -> str:
        """Return the decided mode name."""
        return "abstention" if self.abstain else "synthesis"


@dataclass(frozen=True, slots=True)
class AnswerPolicy:
    """Select evidence with a single similarity floor."""

    floor: float = 0.70
    max_qa: int = field(default=MAX_QA_EVIDENCE)
    max_messages: int = field(default=MAX_MESSAGE_EVIDENCE)

    def select(self, qa: list[Evidence], messages: list[Evidence]) -> EvidenceSelection:
        """Return the evidence for one question.

        Args:
            qa: Retrieved Q&A candidates, best first.
            messages: Retrieved message evidence, best first.

        Returns:
            The selected evidence, or an abstention when none clears the floor.
        """
        usable_qa = [item for item in qa if item.similarity >= self.floor][
            : self.max_qa
        ]
        usable_messages = [item for item in messages if item.similarity >= self.floor][
            : self.max_messages
        ]
        evidence = [*usable_qa, *usable_messages]
        if not evidence:
            return EvidenceSelection(abstain=True)
        return EvidenceSelection(abstain=False, evidence=tuple(evidence))
