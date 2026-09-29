# SPDX-License-Identifier: MIT
"""Answering a question nobody asked the bot to answer.

Proactive answering is the most expensive thing the bot does on its own: it
spends a generation on a guess that the group even wants an answer. Two gates
protect it, and both are here rather than in the connector, because they are
policy and not transport:

1. The listener must have classified the message as a confident question. A
   greeting is not an invitation.
2. The answer must have come back as a synthesis. An abstention or a provider
   failure is never sent to a group that did not ask: uninvited silence is the
   correct behaviour, and "I don't know" uninvited is noise at best.

The work is also metered as its own priority class, so it stops spending before
the background classification and indexing that make tomorrow's answers
possible at all.
"""

from dataclasses import dataclass, field

from knowledge_bot.application.answer_question import AnswerService
from knowledge_bot.application.budget import AiBudget
from knowledge_bot.domain.enums import AiWorkClass, AnswerMode
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.models.questions import AskQuestionResponse
from knowledge_bot.ports.telemetry import NoopTracer, Tracer

#: Status returned when the answer is delivered.
PROACTIVE_ANSWERED = "proactive_answered"
#: Status returned when the bot decided to stay silent.
PROACTIVE_SUPPRESSED = "proactive_suppressed"
#: Status returned when the daily budget does not allow uninvited answers.
PROACTIVE_BUDGET_BLOCKED = "proactive_budget_blocked"


@dataclass(frozen=True, slots=True)
class ProactiveOutcome:
    """What an uninvited answer attempt produced.

    Attributes:
        status: One of the three statuses above.
        mode: The answer mode when there was one, else ``None``.
        response: The answer to deliver, when there is one.
    """

    status: str
    mode: AnswerMode | None = None
    response: AskQuestionResponse | None = None


@dataclass(frozen=True, slots=True)
class ProactiveResponder:
    """Decide whether an unaddressed question deserves an uninvited answer."""

    answer: AnswerService
    budget: AiBudget
    tracer: Tracer = field(default_factory=NoopTracer)

    async def respond(
        self,
        message: NormalizedMessage,
        *,
        space_id: str,
        answer_id: str,
    ) -> ProactiveOutcome:
        """Answer a confident unaddressed question, or stay silent.

        Args:
            message: The message the listener stored and classified.
            space_id: The space whose knowledge may answer it.
            answer_id: The idempotency key this answer is stored under.

        Returns:
            The outcome, carrying the answer only when it should be delivered.
        """
        with self.tracer.span(
            "proactive_decision", space_id=space_id, answer_id=answer_id
        ) as span:
            if not await self.budget.work_allowed(AiWorkClass.PROACTIVE):
                span.set_attributes(
                    {"status": PROACTIVE_BUDGET_BLOCKED, "delivered": False}
                )
                return ProactiveOutcome(PROACTIVE_BUDGET_BLOCKED)
            response = await self.answer.answer_message(
                message, space_id=space_id, answer_id=answer_id
            )
            if response is None:
                span.set_attributes(
                    {
                        "status": PROACTIVE_SUPPRESSED,
                        "delivered": False,
                        "reason": "no_question",
                    }
                )
                return ProactiveOutcome(PROACTIVE_SUPPRESSED)
            if response.mode is not AnswerMode.SYNTHESIS:
                span.set_attributes(
                    {
                        "status": PROACTIVE_SUPPRESSED,
                        "delivered": False,
                        "reason": str(response.mode),
                    }
                )
                return ProactiveOutcome(PROACTIVE_SUPPRESSED, mode=response.mode)
            span.set_attributes(
                {
                    "status": PROACTIVE_ANSWERED,
                    "delivered": True,
                    "mode": str(response.mode),
                }
            )
            return ProactiveOutcome(
                PROACTIVE_ANSWERED, mode=response.mode, response=response
            )
