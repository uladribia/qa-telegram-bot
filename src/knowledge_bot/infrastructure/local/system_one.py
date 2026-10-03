# SPDX-License-Identifier: MIT
"""HTTP client for a local System-One decision service.

The System-One contract is the same whoever answers it: one state, one request,
every question answered together. This adapter knows the route and nothing
else, so pointing ``base_url`` at another conforming service never touches
application code.
"""

from dataclasses import dataclass

import httpx

from knowledge_bot.domain.errors import ModelUnavailableError


@dataclass(frozen=True, slots=True)
class HttpSystemOneTransport:
    """POST one state and its questions to a local decision service."""

    client: httpx.AsyncClient
    base_url: str
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
            ModelUnavailableError: The decision service is unreachable, timed
                out, or answered with something that is not a decision payload.
        """
        url = f"{self.base_url.rstrip('/')}/v1/systemone"
        try:
            response = await self.client.post(
                url,
                json={"model": model, "state": state, "questions": questions},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ModelUnavailableError("decision") from error
        if not isinstance(payload, dict):
            raise ModelUnavailableError("decision")
        return payload
