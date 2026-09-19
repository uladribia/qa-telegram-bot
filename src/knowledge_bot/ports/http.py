# SPDX-License-Identifier: MIT
"""Outbound HTTP port."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class HttpClient(Protocol):
    """Sends JSON requests to external services."""

    async def post_json(
        self, url: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """POST a JSON payload and return the decoded JSON response."""
        ...
