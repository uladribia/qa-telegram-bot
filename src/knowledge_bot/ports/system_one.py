# SPDX-License-Identifier: MIT
"""Port for a System-One decision service."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SystemOneTransport(Protocol):
    """Answers every question about one state in a single request.

    The wire format is the System-One contract: a ``state`` object plus a
    mapping of question name to typed question. Nothing here names a provider,
    so the same port serves Ollama and any other local decision server.
    """

    async def decide(
        self,
        *,
        model: str,
        state: object,
        questions: dict[str, object],
    ) -> dict[str, object]:
        """Send one state and its questions to the decision service.

        Args:
            model: The decision model to answer with.
            state: The state every question is asked about.
            questions: Typed questions keyed by decision name.

        Returns:
            The raw decision payload, to be validated by the caller.
        """
        ...
