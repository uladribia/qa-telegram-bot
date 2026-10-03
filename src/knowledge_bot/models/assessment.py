# SPDX-License-Identifier: MIT
"""Channel-independent decision results for one inbound message.

A message assessment is what the listener learns about one message in a single
decision step: its communicative role, and how relevant it looks as an answer
to each open question candidate. The DTOs carry no transport or provider
concept, so the same shape serves the baseline classifier, a local
System-One model, and the offline dataset tooling.
"""

from dataclasses import dataclass
from typing import Literal

from knowledge_bot.application.classifier import Classification

#: How a candidate question relates to the message being assessed.
CandidateRelation = Literal["explicit_reply", "temporal_window"]


@dataclass(frozen=True, slots=True)
class RetroevalCandidate:
    """One open question a message could answer.

    The current message is the candidate answer; it is never repeated inside
    the candidate, so a request carrying five candidates states the message
    once and asks five independent questions about it.
    """

    candidate_id: str
    question_message_id: str
    question: str
    relation: CandidateRelation


@dataclass(frozen=True, slots=True)
class MessageAssessment:
    """One message's intent decision plus its per-candidate relevance.

    An empty ``pair_relevance`` means the assessment carries no relevance
    opinion, and the deterministic pairing policy decides on its own.
    """

    classification: Classification
    pair_relevance: dict[str, float]
