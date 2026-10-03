# SPDX-License-Identifier: MIT
"""Workers AI transport for the System-One decision contract.

Evaluation-only. User traffic never reaches this adapter: the production
listener stays on the linear classifier and the deterministic pairing policy,
and this module is reachable from exactly one internal, key-gated route that
persists nothing.

There is deliberately no fallback here. A decision evaluation that silently
answers from another model would measure the wrong thing, so a provider or
parsing failure is reported as an evaluation error and nothing else.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from knowledge_bot.domain.errors import ModelUnavailableError
from knowledge_bot.infrastructure.cloudflare.workers_ai import AiRunner

#: The only model an evaluation may decide with.
EVAL_DECISION_MODEL = "@cf/cloudflare/clef-flash"


@dataclass(frozen=True, slots=True)
class WorkersAISystemOneTransport:
    """Ask a Workers AI decision model one state and its questions."""

    runner: AiRunner
    timeout_seconds: float = 20.0

    async def decide(
        self,
        *,
        model: str,
        state: object,
        questions: dict[str, object],
    ) -> dict[str, object]:
        """Send one decision request and return the raw payload.

        Args:
            model: The decision model to answer with.
            state: The state every question is asked about.
            questions: Typed questions keyed by decision name.

        Returns:
            The raw decision payload, for the application parser to validate.

        Raises:
            ModelUnavailableError: The binding failed, timed out, or answered
                with something that is not a decision payload.
        """
        started = asyncio.get_running_loop().time()
        try:
            response = await asyncio.wait_for(
                self.runner.run(model, {"state": state, "questions": questions}),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as error:
            logging.getLogger("knowledge_bot.ai").warning(
                "decision_call_failed",
                extra={"model": model, "error": "timeout"},
            )
            raise ModelUnavailableError("decision") from error
        except Exception as error:
            logging.getLogger("knowledge_bot.ai").warning(
                "decision_call_failed",
                extra={"model": model, "error": type(error).__name__},
            )
            raise ModelUnavailableError("decision") from error
        logging.getLogger("knowledge_bot.ai").info(
            "decision_call_completed",
            extra={
                "model": model,
                "questions": len(questions),
                "duration_ms": round(
                    (asyncio.get_running_loop().time() - started) * 1000, 2
                ),
            },
        )
        return _payload(response)

    @property
    def model(self) -> str:
        """The model an evaluation decides with."""
        return EVAL_DECISION_MODEL


def _payload(response: object) -> dict[str, object]:
    """Return the decision payload from a binding response.

    Args:
        response: Whatever the binding returned: a mapping, or a JSON string.

    Returns:
        The decoded decision payload.

    Raises:
        ModelUnavailableError: The response is neither a mapping nor a JSON
            object, so there is nothing for the parser to validate.
    """
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except ValueError as error:
            raise ModelUnavailableError("decision") from error
        response = decoded
    if not isinstance(response, dict):
        raise ModelUnavailableError("decision")
    return {str(key): value for key, value in response.items()}
